"""Clean AWS documentation Markdown and split it into chunks by heading.

Fenced code blocks (CLI commands, JSON policies) are never split or rewritten.
"""

import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 1200
MIN_CHUNK_CHARS = 60

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_ANCHOR = re.compile(r"<a name=\"[^\"]*\"></a>")
_LINK = re.compile(r"\[([^\]\[]*(?:\\\[[^\]]*\\\][^\]\[]*)*)\]\([^)\s]*\)")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>|~\"'$&=:/,;?%@^])")
_SEE_WEBSITE = re.compile(r"\[?\\?\[See the AWS documentation website for more details\\?\]\]?(\([^)]*\))?")
_TOPIC_LINK = re.compile(r"^\s*[+*-]\s+\[.*\]\((#[^)]*|[^)]*\.md[^)]*)\)\s*$")


@dataclass(frozen=True)
class Chunk:
    section: str  # heading path, e.g. "Troubleshoot connecting > Connection timed out"
    content: str


def _clean_line(line: str) -> str:
    line = _ANCHOR.sub("", line)
    line = _SEE_WEBSITE.sub("", line)
    line = _LINK.sub(r"\1", line)
    line = _ESCAPE.sub(r"\1", line)
    line = re.sub(r"^(\s*)\+ ", r"\1- ", line)
    return line.rstrip()


def clean(markdown: str) -> str:
    """Remove documentation-site scaffolding while leaving code blocks untouched."""
    out: list[str] = []
    in_code = False
    skipping_topics = False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            skipping_topics = False
            out.append(line.rstrip())
            continue
        if in_code:
            out.append(line.rstrip())
            continue
        # A "**Topics**" list is a table of contents made only of links to other sections or pages.
        if line.strip() == "**Topics**":
            skipping_topics = True
            continue
        if skipping_topics:
            if _TOPIC_LINK.match(line) or not line.strip():
                continue
            skipping_topics = False
        out.append(_clean_line(line))
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _blocks(body: str) -> list[str]:
    """Split a section into paragraphs, keeping each fenced code block whole."""
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            current.append(line)
            continue
        if not in_code and not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _pack(blocks: list[str], limit: int) -> list[str]:
    """Greedily join blocks into pieces of at most `limit` characters. An oversized block stays whole."""
    pieces: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > limit:
            pieces.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        pieces.append(current)
    return pieces


def split(markdown: str, *, max_chars: int = MAX_CHUNK_CHARS) -> tuple[str, list[Chunk]]:
    """Return the page title and its chunks. `markdown` should already be cleaned."""
    title = ""
    levels = [""] * 4  # current heading text at each level, "" where there is none
    sections: list[tuple[str, list[str]]] = []
    lines: list[str] = []
    in_code = False

    def close() -> None:
        if lines:
            sections.append((" > ".join(text for text in levels if text), list(lines)))
            lines.clear()

    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
        heading = None if in_code else _HEADING.match(line)
        if heading:
            close()
            level = len(heading.group(1))
            text = heading.group(2).strip().strip("[]").strip()  # console tabs are written "[ New console ]"
            if level == 1 and not title:
                title = text
            levels[level - 1:] = [text] + [""] * (4 - level)
        else:
            lines.append(line)
    close()

    chunks = [
        Chunk(section=section or title, content=piece)
        for section, body in sections
        for piece in _pack(_blocks("\n".join(body)), max_chars)
        if len(piece) >= MIN_CHUNK_CHARS
    ]
    return title, chunks
