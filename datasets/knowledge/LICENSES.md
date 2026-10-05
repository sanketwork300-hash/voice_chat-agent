# Licenses of the knowledge-base sources

The knowledge base is built from the archived `awsdocs` repositories on GitHub. The pages are downloaded by `scripts/ingest_docs.py` into `datasets/knowledge/corpus/`, which is not committed to this repository; nothing from them is redistributed here. Each answer that uses a page links to it.

The license statements below were read from each repository at the pinned commit listed in `manifest.yaml`.

| Repository | Pinned commit | Documentation license | Sample-code license | Where stated |
|---|---|---|---|---|
| `awsdocs/amazon-ec2-user-guide` | `e3707c43` (2023-06-15) | CC BY-SA 4.0 | Modified MIT (MIT-0) | `LICENSE-SUMMARY`, `LICENSE`, `LICENSE-SAMPLECODE` |
| `awsdocs/amazon-s3-userguide` | `cc53dd6a` (2023-06-15) | CC BY-SA 4.0 | MIT-0 | `LICENSE-SUMMARY`, `LICENSE`, `LICENSE-SAMPLECODE` |
| `awsdocs/aws-lambda-developer-guide` | `03f7fcb0` (2023-10-30) | CC BY-SA 4.0 | Modified MIT (MIT-0) | `LICENSE-SUMMARY`, `LICENSE`, `LICENSE-SAMPLECODE` |
| `awsdocs/amazon-cloudwatch-user-guide` | `cc40332f` (2023-06-15) | CC BY-SA 4.0 | Modified MIT (MIT-0) | `LICENSE-SUMMARY`, `LICENSE`, `LICENSE-SAMPLECODE` |
| `awsdocs/aws-cost-management-user-guide` | `a3d0766b` (2023-06-08) | CC BY-SA 4.0 | Modified MIT (MIT-0) | `LICENSE-SUMMARY`, `LICENSE`, `LICENSE-SAMPLECODE` |
| `awsdocs/iam-user-guide` | `e2b8637a` (2023-06-15) | Not stated | Modified MIT (MIT-0) | `LICENSE`, `README.md` |

## IAM user guide

The IAM repository contains only a modified-MIT `LICENSE` file, and its README says that license covers the sample code. It has no `LICENSE-SUMMARY` and does not state a license for the documentation text itself. The other five repositories all state CC BY-SA 4.0 for documentation, but that has not been confirmed for this one.

Check this before redistributing IAM page content, for example before committing the corpus or publishing answers that quote it at length. To leave IAM out, delete its block from `manifest.yaml` and run `make ingest`.

## Obligations under CC BY-SA 4.0

Attribution and share-alike apply if the documentation text, or text adapted from it, is distributed. The chat page attributes each cited page by title with a link to the exact source file at its pinned commit.
