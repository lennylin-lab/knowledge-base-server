"""Markdown-aware chunking — pure, deterministic, no I/O.

Line-based ATX-heading splitting (see design.md: markdown-it-py deliberately
deferred) with a CommonMark-subset fenced-code-block state machine: a `#`
comment inside a fence is code content, not a section boundary, and fence
content is never mutated. Sections travel with their heading, small sections
merge toward the target size, oversized sections split at paragraph (then
line) boundaries — and an oversized fence is closed and re-opened at piece
boundaries so every piece stays independently valid Markdown.
"""

from __future__ import annotations

from dataclasses import dataclass

# Blank line between joined units — both section- and paragraph-level.
_SEPARATOR = "\n\n"

# Breadcrumb separator between ancestor headings in `Chunk.heading_path`.
_HEADING_PATH_SEPARATOR = " > "


@dataclass(frozen=True)
class Chunk:
    """One chunk. `text` is exactly what PG stores and search returns;
    `heading_path` is the ancestor breadcrumb (retrieval signal for both the
    embedding input and the ES `heading_path` field) — never user payload."""

    text: str
    heading_path: str


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
    """`^#{1,6} ` — an ATX heading opening a new section.

    Callers must ensure the line is not inside an open fenced code block
    (see `_FenceState`); a `#` comment in code must not read as a heading.
    """
    if not line.startswith("#"):
        return False
    remainder = line.lstrip("#")
    hashes = len(line) - len(remainder)
    return 1 <= hashes <= 6 and remainder.startswith(" ")


def _heading_text(line: str) -> str:
    """Heading content for the breadcrumb: opening hashes and any CommonMark
    closing sequence (trailing `#`s preceded by whitespace) stripped — so
    `## Done ##` indexes as `Done` while `## C#` keeps its `#`."""
    text = line.lstrip("#").strip()
    if text.endswith("#"):
        without_hashes = text.rstrip("#")
        if not without_hashes or without_hashes[-1].isspace():
            text = without_hashes.rstrip()
    return text


@dataclass(frozen=True)
class _Fence:
    """An open fenced code block (CommonMark subset)."""

    char: str  # "`" or "~"
    length: int  # run length of the opening marker (>= 3)
    info: str  # raw remainder of the opening line ("" when none)

    @property
    def marker(self) -> str:
        """Re-opening line content: the original marker plus its info string."""
        return f"{self.char * self.length}{self.info}"

    @property
    def close(self) -> str:
        """Closing marker: a run of the opening character, no info string."""
        return self.char * self.length


def _fence_opens(line: str) -> _Fence | None:
    """Opening fence line: <=3 leading spaces, then >=3 identical backtick or
    tilde characters, then an info string. A backtick fence's info string may
    not contain a backtick (CommonMark)."""
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    rest = line[indent:]
    char = rest[:1]
    if char not in ("`", "~"):
        return None
    run = len(rest) - len(rest.lstrip(char))
    if run < 3:
        return None
    info = rest[run:]
    if char == "`" and "`" in info:
        return None
    return _Fence(char=char, length=run, info=info)


def _fence_closes(line: str, fence: _Fence) -> bool:
    """Closing fence line: <=3 leading spaces, a run of the same character at
    least as long as the opening run, then only whitespace. A shorter run (or
    an info string) does not close the block."""
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return False
    rest = line[indent:]
    if not rest.startswith(fence.char):
        return False
    run = len(rest) - len(rest.lstrip(fence.char))
    if run < fence.length:
        return False
    return not rest[run:].strip()


class _FenceState:
    """Fence open/closed cursor over a line walk.

    `feed` consumes one line and returns whether the walk is inside fence
    content after it: True for the opening line and interior lines, False
    once the closing line ends the block (the closing line itself is never a
    heading or blank, so the distinction only matters for interior lines).
    """

    __slots__ = ("fence",)

    def __init__(self) -> None:
        self.fence: _Fence | None = None

    def feed(self, line: str) -> bool:
        if self.fence is None:
            self.fence = _fence_opens(line)
            return self.fence is not None
        if _fence_closes(line, self.fence):
            self.fence = None
            return False
        return True


class _HeadingStack:
    """Ancestor breadcrumb builder keyed by ATX level.

    A heading of level N replaces same-or-deeper entries and is pushed on top;
    a shallower heading pops everything below it. The path includes the
    section's own heading (the PRD signal: a bare fragment deeper in a section
    must still say where it lives); only the preamble yields "".
    """

    __slots__ = ("_levels",)

    def __init__(self) -> None:
        self._levels: dict[int, str] = {}

    def enter(self, line: str) -> str:
        """Feed a section's first line and return the path in force for it."""
        if _is_atx_heading(line):
            hashes = len(line) - len(line.lstrip("#"))
            for level in [key for key in self._levels if key >= hashes]:
                del self._levels[level]
            self._levels[hashes] = _heading_text(line)
        return _HEADING_PATH_SEPARATOR.join(self._levels[level] for level in sorted(self._levels))


def _split_sections(lines: list[str]) -> list[str]:
    """Group lines into sections; each heading travels with what follows.

    Fence-aware: an ATX-heading-looking line inside an open fenced code block
    is code content, not a section boundary. Whitespace-only sections (blank
    preamble, trailing blanks) are dropped.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    fences = _FenceState()
    for line in lines:
        if not fences.feed(line) and _is_atx_heading(line):
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    groups.append(current)
    return [stripped for group in groups if (stripped := "\n".join(group).strip())]


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank-line runs; blank-only input yields no paragraphs.

    Fence-aware: a blank line inside an open fenced code block is part of the
    block (joining here is by "\n", so fence content stays byte-preserved).
    """
    paragraphs: list[str] = []
    current: list[str] = []
    fences = _FenceState()
    for line in text.split("\n"):
        if fences.feed(line) or line.strip():
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


def _pack_lines(lines: list[str], *, max_size: int) -> list[list[str]]:
    """Greedy line packing: groups of lines that stay within `max_size` when
    joined by newlines. A line longer than `max_size` is hard-sliced, each
    slice becoming its own group."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > max_size:
            if current:
                groups.append(current)
                current, size = [], 0
            groups.extend([slice] for slice in _hard_slice(line, max_size=max_size))
            continue
        if not current:
            current, size = [line], len(line)
        elif size + 1 + len(line) <= max_size:
            current.append(line)
            size += 1 + len(line)
        else:
            groups.append(current)
            current, size = [line], len(line)
    if current:
        groups.append(current)
    return groups


def _repair_fences(groups: list[list[str]]) -> list[str]:
    """Close-and-reopen fences at piece boundaries so every piece is valid.

    When a piece boundary lands inside an open fence, the piece gets the
    fence's closing marker appended and the next piece the opening marker
    (with the original info string) prepended. Repair markers are the only
    bytes ever added; they are applied after packing, so a repaired piece can
    exceed `max_size` by one marker (a chunk-quality target, not a hard
    external limit — see design.md).
    """
    pieces: list[str] = []
    fences = _FenceState()
    for group in groups:
        lines: list[str] = [fences.fence.marker] if fences.fence is not None else []
        for line in group:
            fences.feed(line)
            lines.append(line)
        if fences.fence is not None:
            lines.append(fences.fence.close)
        pieces.append("\n".join(lines))
    return pieces


def _split_by_lines(text: str, *, max_size: int) -> list[str]:
    """Pack lines into pieces of at most `max_size` characters, repairing
    fences that a piece boundary cuts through."""
    return _repair_fences(_pack_lines(text.split("\n"), max_size=max_size))


def _split_oversized(section: str, *, max_size: int) -> list[str]:
    """Split a too-long section at paragraph boundaries, then line boundaries.

    Paragraph boundaries are always outside any fence (blank lines inside a
    fence never break paragraphs), so only the line-level split can cut a
    fence — and `_split_by_lines` repairs that.
    """
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


def chunk_markdown_structured(body: str, *, target: int = 800, max_size: int = 1600) -> list[Chunk]:
    """Split markdown `body` (front matter excluded) into chunks with
    breadcrumb metadata.

    Packing is deterministic and pure: consecutive sections pack greedily
    while the combined size stays within `max_size` and the current chunk has
    not yet reached `target` (a section boundary is always preferred over
    splitting mid-section). A single section longer than `max_size` splits at
    paragraph boundaries, then at line (and finally character) boundaries.
    Whitespace is never chunked; an empty or front-matter-only body yields
    `[]`. A chunk merged from several sections carries the heading path of the
    section that opens it.
    """
    content = strip_front_matter(body)
    if not content.strip():
        return []

    units: list[tuple[str, str]] = []  # (unit text, heading path)
    stack = _HeadingStack()
    for section in _split_sections(content.split("\n")):
        path = stack.enter(section.split("\n", 1)[0])
        if len(section) <= max_size:
            units.append((section, path))
        else:
            units.extend((piece, path) for piece in _split_oversized(section, max_size=max_size))

    chunks: list[Chunk] = []
    current = ""
    current_path = ""
    for unit, path in units:
        if not current:
            current, current_path = unit, path
        elif len(current) + len(_SEPARATOR) + len(unit) <= max_size and len(current) < target:
            current = f"{current}{_SEPARATOR}{unit}"
        else:
            chunks.append(Chunk(text=current, heading_path=current_path))
            current, current_path = unit, path
    if current:
        chunks.append(Chunk(text=current, heading_path=current_path))
    return chunks


def chunk_markdown(body: str, *, target: int = 800, max_size: int = 1600) -> list[str]:
    """Split markdown `body` into chunk texts (text-only view of
    `chunk_markdown_structured`).

    Stays the summarize-path entry point (`services/agents.py`): semantics
    unchanged apart from the fenced-code-block fixes.
    """
    return [
        chunk.text for chunk in chunk_markdown_structured(body, target=target, max_size=max_size)
    ]
