"""Chunker units: pure, offline, deterministic (no fixtures needed)."""

from __future__ import annotations

from app.rag.chunker import chunk_markdown, chunk_markdown_structured, strip_front_matter


def test_empty_body_yields_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("\n\n   \n") == []


def test_front_matter_only_body_yields_no_chunks():
    assert chunk_markdown("---\ntitle: x\ntags: [a]\n---\n") == []
    # Content that never follows the closing fence.
    assert chunk_markdown("---\ntitle: x\n---") == []


def test_front_matter_is_excluded_from_chunks():
    body = "---\ntitle: Secret Title\ntags: [private]\n---\n\n# Heading\n\nBody text."

    chunks = chunk_markdown(body)

    assert len(chunks) == 1
    assert "Secret Title" not in chunks[0]
    assert "private" not in chunks[0]
    assert "# Heading" in chunks[0]
    assert "Body text." in chunks[0]


def test_unclosed_front_matter_fence_is_treated_as_content():
    body = "---\ntitle: x\n\nstill content"

    assert chunk_markdown(body) == [body]


def test_heading_travels_with_its_section():
    # First section is >= target (800) so the two sections cannot merge.
    alpha = "# Alpha\n\n" + "a" * 900
    body = f"{alpha}\n\n# Beta\n\nbeta text"

    assert chunk_markdown(body) == [alpha, "# Beta\n\nbeta text"]


def test_lines_before_the_first_heading_form_their_own_section():
    preamble = "p" * 900  # >= target keeps it from merging into the next section
    body = f"{preamble}\n\n# Heading\n\nbody"

    assert chunk_markdown(body) == [preamble, "# Heading\n\nbody"]


def test_consecutive_small_sections_merge_into_one_chunk():
    body = "\n\n".join(f"# Section {i}\n\n{'x' * 100}" for i in range(3))

    chunks = chunk_markdown(body)

    assert len(chunks) == 1
    for i in range(3):
        assert f"# Section {i}" in chunks[0]


def test_packing_stops_at_target_even_when_max_size_would_allow_more():
    body = "\n\n".join(
        [
            f"# A\n\n{'a' * 500}",
            f"# B\n\n{'b' * 400}",
            f"# C\n\n{'c' * 100}",
        ]
    )

    chunks = chunk_markdown(body, target=800, max_size=1600)

    # A+B is 912 >= target 800, so C starts a new chunk despite 1021 <= 1600.
    assert len(chunks) == 2
    assert "a" * 500 in chunks[0]
    assert "b" * 400 in chunks[0]
    assert chunks[1] == f"# C\n\n{'c' * 100}"


def test_oversized_section_splits_at_paragraph_boundaries():
    paragraphs = [f"para {i} " + "p" * 700 for i in range(3)]
    body = "# Big\n\n" + "\n\n".join(paragraphs)

    chunks = chunk_markdown(body, target=800, max_size=1600)

    assert all(len(chunk) <= 1600 for chunk in chunks)
    joined = "\n\n".join(chunks)
    for paragraph in paragraphs:
        assert paragraph in joined
    # The heading stays with the first piece.
    assert chunks[0].startswith("# Big\n")


def test_single_paragraph_longer_than_max_splits_at_line_boundaries():
    lines = ["l" * 300] * 12  # 3600+ chars, no blank line anywhere
    body = "# Huge\n\n" + "\n".join(lines)

    chunks = chunk_markdown(body, target=800, max_size=1600)

    assert all(len(chunk) <= 1600 for chunk in chunks)
    assert sum(chunk.count("l" * 300) for chunk in chunks) == 12


def test_single_line_longer_than_max_is_hard_sliced():
    body = "# Wide\n\n" + "z" * 3500

    chunks = chunk_markdown(body, target=800, max_size=1600)

    # The heading cannot fit next to a full 1600-char slice, so it stands alone.
    assert [len(chunk) for chunk in chunks] == [len("# Wide"), 1600, 1600, 300]


def test_chunk_never_exceeds_max_size_for_many_small_sections():
    body = "\n\n".join(f"# S{i}\n\n{'y' * 300}" for i in range(20))

    chunks = chunk_markdown(body, target=800, max_size=1600)

    assert len(chunks) > 1
    assert all(chunk and len(chunk) <= 1600 for chunk in chunks)


def test_whitespace_only_sections_are_dropped():
    real = "# Real\n\n" + "a" * 900
    body = f"{real}\n\n\n   \n\n# Next\n\nmore"

    assert chunk_markdown(body) == [real, "# Next\n\nmore"]


def test_seven_hashes_is_not_a_heading():
    body = "####### not a heading\n\n####### also not\n\nreal text"

    assert chunk_markdown(body) == [body]


def test_heading_without_space_is_not_a_heading():
    body = "#NotAHeading\n\ntext"

    assert chunk_markdown(body) == [body]


def test_chunking_is_deterministic():
    body = "\n\n".join(f"# S{i}\n\n{'d' * 200}" for i in range(10))

    assert chunk_markdown(body) == chunk_markdown(body)


def test_section_exactly_max_size_stays_whole():
    section = "# T\n\n" + "e" * 1593  # 1598 <= 1600

    assert chunk_markdown(section, target=800, max_size=1600) == [section]


def test_strip_front_matter_variants():
    assert strip_front_matter("---\na: 1\n---\nbody") == "body"
    assert strip_front_matter("no fence") == "no fence"
    assert strip_front_matter("---\na: 1\n---") == ""
    # Whitespace tolerance around the fences.
    assert strip_front_matter("--- \na: 1\n --- \nbody") == "body"


# --- fenced code blocks (regressions for the P0-A chunker defects) ---


def test_hash_comment_inside_fence_is_not_a_heading():
    # P0-A reproducer: `# 步骤 N` comments at column 0 match `^#{1,6} ` but are
    # code content — the document's only section boundary is `## Redis …`.
    lines = "\n".join(f"# 步骤 {i}" for i in range(30))
    body = f"## Redis 缓存实践\n\n```python\n{lines}\nprint('done')\n```"

    assert chunk_markdown(body) == [body]


def test_fence_content_is_byte_preserved():
    # The P0-A mutation: a misdetected comment used to be split into its own
    # section and rejoined with the blank-line separator, inserting a blank
    # line right after the opening fence.
    body = "```yaml\n# Redis 服务配置\nredis:\n  host: localhost\n```"

    assert chunk_markdown(body) == [body]


def test_oversized_fence_splits_into_individually_valid_blocks():
    lines = [f"line {i:03d} " + "x" * 60 for i in range(40)]
    body = "```python\n" + "\n".join(lines) + "\n```"

    chunks = chunk_markdown(body)

    assert len(chunks) >= 2
    for chunk in chunks:
        # Every piece is independently valid Markdown: own opening fence with
        # the original info string, own closing fence, no half-open fences.
        assert chunk.startswith("```python")
        assert chunk.endswith("```")
        assert chunk.count("```") == 2
        # Repair markers are added after packing, so a piece may overshoot
        # max_size by one marker (10 chars for "```python\n").
        assert len(chunk) <= 1600 + 16
    joined = "\n".join(chunks)
    for line in lines:
        assert joined.count(line) == 1  # every code line survives exactly once


def test_tilde_fence_and_long_backtick_fence_are_honored():
    tilde_body = "~~~\n# not a heading\n``` neither is this a close\n~~~"
    long_body = "````text\n```\n# inner heading-like\n```\n````"

    assert chunk_markdown(tilde_body) == [tilde_body]
    assert chunk_markdown(long_body) == [long_body]


def test_short_closing_run_does_not_close_a_longer_fence():
    body = "````markdown\n# heading-like text\n```\nstill inside\n````"

    assert chunk_markdown(body) == [body]


def test_unclosed_fence_runs_to_end_of_document():
    body = "## Doc\n\n```python\nimport os\n# TODO: fix this\nprint(os)"

    assert chunk_markdown(body) == [body]


def test_heading_path_tracks_the_heading_stack():
    sections = [
        "intro " + "i" * 900,  # preamble: no heading yet
        "# Top\n\n" + "a" * 900,
        "## Sub\n\n" + "b" * 900,
        "## Other\n\n" + "c" * 900,  # same level replaces the previous entry
        "### Deep\n\n" + "d" * 900,
        "# Back\n\n" + "e" * 900,  # level 1 resets the whole stack
    ]
    body = "\n\n".join(sections)

    chunks = chunk_markdown_structured(body)

    # Every section is >= target, so each lands in its own chunk.
    assert [chunk.heading_path for chunk in chunks] == [
        "",
        "Top",
        "Top > Sub",
        "Top > Other",
        "Top > Other > Deep",
        "Back",
    ]


def test_heading_path_is_not_prepended_to_chunk_text():
    body = f"# Parent\n\n{'a' * 900}\n\n## Child\n\n{'b' * 900}"

    chunks = chunk_markdown_structured(body)

    assert [chunk.text for chunk in chunks] == chunk_markdown(body)
    assert chunks[0].heading_path == "Parent"
    assert chunks[1].heading_path == "Parent > Child"
    # The heading line travels inline (existing behavior); the breadcrumb is
    # retrieval signal only and must not leak into the stored text.
    assert chunks[1].text == f"## Child\n\n{'b' * 900}"
    assert "Parent" not in chunks[1].text


def test_heading_path_strips_closing_sequence_but_keeps_content_hashes():
    # CommonMark: a trailing #-run preceded by whitespace is a closing
    # sequence, not content; a # glued to a word is content (`## C#`).
    # Both headings are level 2, so the second replaces the first in the stack.
    body = f"## Done ##\n\n{'a' * 900}\n\n## C#\n\n{'b' * 900}"

    chunks = chunk_markdown_structured(body)

    assert [chunk.heading_path for chunk in chunks] == ["Done", "C#"]
    # The stored text keeps the heading lines verbatim.
    assert chunks[0].text.startswith("## Done ##")
