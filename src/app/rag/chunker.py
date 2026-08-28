"""Markdown-aware chunking — pure, deterministic, no I/O.

Line-based ATX-heading splitting (see design.md: markdown-it-py deliberately
deferred). Sections travel with their heading, small sections merge toward the
target size, oversized sections split at paragraph (then line) boundaries.
"""

from __future__ import annotations

# Blank line between joined units — both section- and paragraph-level.
_SEPARATOR = "\n\n"


def strip_front_matter(body: str) -> str:
    """Drop a leading `---`-fenced front-matter block if present.

    Title/tags are indexed as fields, never as chunk text. An unclosed fence
    is treated as ordinary content (the service layer already rejects
    unparseable front matter, this is defense in depth).
    """
    lines = body.split("\n")
    if not lines or lines[0].strip() != "---":
        return body
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :])
    return body


def _is_atx_heading(line: str) -> bool:
    """`^#{1,6} ` — an ATX heading opening a new section."""
    if not line.startswith("#"):
        return False
    remainder = line.lstrip("#")
    hashes = len(line) - len(remainder)
    return 1 <= hashes <= 6 and remainder.startswith(" ")


def _split_sections(lines: list[str]) -> list[str]:
    """Group lines into sections; each heading travels with what follows.

    Whitespace-only sections (blank preamble, trailing blanks) are dropped.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _is_atx_heading(line):
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    groups.append(current)
    return [stripped for group in groups if (stripped := "\n".join(group).strip())]


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank-line runs; blank-only input yields no paragraphs."""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _hard_slice(line: str, *, max_size: int) -> list[str]:
    """Character-level fallback: a single line longer than `max_size`."""
    return [line[i : i + max_size] for i in range(0, len(line), max_size)]


def _split_by_lines(text: str, *, max_size: int) -> list[str]:
    """Pack lines into pieces of at most `max_size` characters."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > max_size:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_slice(line, max_size=max_size))
            continue
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= max_size:
            current = f"{current}\n{line}"
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def _split_oversized(section: str, *, max_size: int) -> list[str]:
    """Split a too-long section at paragraph boundaries, then line boundaries."""
    pieces: list[str] = []
    current = ""
    for paragraph in _split_paragraphs(section):
        if len(paragraph) > max_size:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_split_by_lines(paragraph, max_size=max_size))
            continue
        if not current:
            current = paragraph
        elif len(current) + len(_SEPARATOR) + len(paragraph) <= max_size:
            current = f"{current}{_SEPARATOR}{paragraph}"
        else:
            pieces.append(current)
            current = paragraph
    if current:
        pieces.append(current)
    return pieces


def chunk_markdown(body: str, *, target: int = 800, max_size: int = 1600) -> list[str]:
    """Split markdown `body` (front matter excluded) into chunk texts.

    Deterministic and pure: consecutive sections pack greedily while the
    combined size stays within `max_size` and the current chunk has not yet
    reached `target` (a section boundary is always preferred over splitting
    mid-section). A single section longer than `max_size` splits at paragraph
    boundaries, then at line (and finally character) boundaries. Whitespace is
    never chunked; an empty or front-matter-only body yields `[]`.
    """
    content = strip_front_matter(body)
    if not content.strip():
        return []

    units: list[str] = []
    for section in _split_sections(content.split("\n")):
        if len(section) <= max_size:
            units.append(section)
        else:
            units.extend(_split_oversized(section, max_size=max_size))

    chunks: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + len(_SEPARATOR) + len(unit) <= max_size and len(current) < target:
            current = f"{current}{_SEPARATOR}{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks
