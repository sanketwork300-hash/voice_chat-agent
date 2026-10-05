"""Cleaning, chunking, the manifest, and similarity search, without Postgres or a real embedding model."""

import asyncio
import re

from backend.rag import chunking, corpus
from backend.rag.knowledge import MemoryKnowledgeBase
from tests.fakes import FakeEmbedder, doc_chunk

PAGE = """# Troubleshoot connecting to your instance<a name="TroubleshootingInstancesConnecting"></a>

The following can help you troubleshoot connecting to your Linux instance\\.

**Topics**
+ [Common causes](#causes)
+ [Connection timed out](other-page.md)

## Common causes<a name="causes"></a>

Ensure that the security group allows SSH traffic\\. See [Authorize inbound traffic](authorizing-access-to-an-instance.md)\\.
+ Check port 22\\.
[\\[See the AWS documentation website for more details\\]](http://docs.aws.amazon.com/x.html)

### Using the CLI<a name="cli"></a>

Run the following command\\.

```
aws ec2 authorize-security-group-ingress --group-id sg-123 \\
    --protocol tcp --port 22 --cidr 203.0.113.25/32

# a comment that is not a heading
```

#### [ New console ]

Open the console\\. Choose **Instances**, and then select your instance to see its details\\.

#### [ Old console ]

Open the old console\\. Choose **Instances**, and then select your instance to see its details\\.
"""


def test_clean_removes_scaffolding_but_not_code():
    text = chunking.clean(PAGE)
    assert "<a name" not in text and "**Topics**" not in text and "Connection timed out" not in text
    assert "See the AWS documentation website" not in text
    assert "See Authorize inbound traffic." in text          # link text kept, target dropped
    assert "- Check port 22." in text                         # bullets normalised, escapes removed
    # Inside the code block the trailing backslash and the '#' line are untouched.
    assert "--group-id sg-123 \\\n    --protocol tcp" in text
    assert "# a comment that is not a heading" in text


def test_split_follows_headings_and_keeps_code_blocks_whole():
    title, chunks = chunking.split(chunking.clean(PAGE))
    assert title == "Troubleshoot connecting to your instance"
    sections = [c.section for c in chunks]
    assert sections == [
        "Troubleshoot connecting to your instance",
        "Troubleshoot connecting to your instance > Common causes",
        "Troubleshoot connecting to your instance > Common causes > Using the CLI",
        "Troubleshoot connecting to your instance > Common causes > Using the CLI > New console",
        "Troubleshoot connecting to your instance > Common causes > Using the CLI > Old console",
    ]
    cli = chunks[2].content
    assert cli.count("```") == 2 and "not a heading" in cli


def test_long_sections_are_split_between_paragraphs_never_inside_code():
    code = "```\n" + "\n\n".join(f'{{"Sid": "Statement{i}", "Effect": "Allow"}}' for i in range(40)) + "\n```"
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(8))
    _, chunks = chunking.split(f"# Policies\n\n{paragraphs}\n\n{code}\n\nAfter the code.", max_chars=600)
    assert len(chunks) > 4
    assert all(c.content.count("```") % 2 == 0 for c in chunks)
    whole = next(c for c in chunks if "Statement0" in c.content)
    assert "Statement39" in whole.content and len(whole.content) > 600   # oversized code stays in one piece
    assert all(len(c.content) <= 600 for c in chunks if "```" not in c.content)


def test_manifest_is_well_formed():
    specs = corpus.load_manifest()
    assert len(specs) > 50
    assert {s.service for s in specs} == {"ec2", "s3", "lambda", "iam", "cloudwatch", "billing"}
    for spec in specs:
        assert re.fullmatch(r"[0-9a-f]{40}", spec.commit), spec.document_id
        assert spec.file.endswith(".md") and spec.category
        assert spec.source_url == f"https://github.com/{spec.repo}/blob/{spec.commit}/doc_source/{spec.file}"
    assert len({s.document_id for s in specs}) == len(specs)


def knowledge() -> MemoryKnowledgeBase:
    kb = MemoryKnowledgeBase(FakeEmbedder())
    kb.add([
        doc_chunk("ec2/ssh#0", "ec2", "Authorize inbound traffic", "Add an inbound rule to allow SSH on port 22."),
        doc_chunk("s3/naming#0", "s3", "Bucket naming rules", "Bucket names must be 3 to 63 characters long."),
        doc_chunk("s3/policy#0", "s3", "Bucket policies", "A bucket policy grants or denies access to a bucket."),
    ])
    return kb


def search(query: str, service: str | None = None, min_score: float = 0.3):
    return asyncio.run(knowledge().search(query, service=service, k=2, min_score=min_score))


def test_search_returns_the_most_similar_chunks_best_first():
    found = search("how do I allow SSH on port 22 with an inbound rule")
    assert found[0].chunk_id == "ec2/ssh#0" and 0.3 <= found[0].score <= 1.0
    assert [f.score for f in found] == sorted((f.score for f in found), reverse=True)


def test_search_returns_nothing_when_nothing_is_relevant():
    assert search("what is the best pizza topping") == []
    assert search("how do I allow SSH on port 22", min_score=0.99) == []


def test_service_filter_is_preferred_but_falls_back_to_all_services():
    only_s3 = search("bucket names characters long", service="s3")
    assert only_s3 and all(f.service == "s3" for f in only_s3)
    # Asked in an S3 conversation, but only the EC2 page is relevant.
    assert search("allow SSH on port 22 inbound rule", service="s3")[0].chunk_id == "ec2/ssh#0"
