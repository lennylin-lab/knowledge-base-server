"""Pure chat-session helpers (offline): history-window selection, title
derivation, message-history rebuilding — no DB, no LLM, no infrastructure."""

from __future__ import annotations

from uuid import uuid4

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from app.models.chat import ChatMessage, MessageRole
from app.services.chat import TRUNCATION_MARKER, select_history_window, to_message_history
from app.services.session import derive_title


def _msg(role: MessageRole, content: str) -> ChatMessage:
    """An unsaved message row — the window only reads role/content."""
    return ChatMessage(session_id=uuid4(), role=role, content=content)


def _newest_first(*turns: tuple[str, str], trailing_user: str | None = None) -> list[ChatMessage]:
    """Newest-first message list from chronological (question, answer) pairs."""
    messages: list[ChatMessage] = []
    if trailing_user is not None:
        # A failed run's unanswered question is the newest message.
        messages.append(_msg(MessageRole.USER, trailing_user))
    for question, answer in reversed(turns):
        messages.append(_msg(MessageRole.ASSISTANT, answer))
        messages.append(_msg(MessageRole.USER, question))
    return messages


def _contents(messages: list[ChatMessage]) -> list[str]:
    return [message.content for message in messages]


def _select(
    messages: list[ChatMessage], *, budget: int, per_turn_cap: int = 0
) -> list[ChatMessage]:
    """select_history_window with the offline `len` measure; the default
    `per_turn_cap <= 0` disables the guardrail — the all-or-nothing walk the
    original selection tests pin."""
    return select_history_window(messages, budget=budget, measure=len, per_turn_cap=per_turn_cap)


def _cjk_aware_measure(text: str) -> int:
    """Deterministic fake token measure with real mixed-script shape: CJK
    characters cost one token each, other text one per four characters."""
    cjk = sum(1 for char in text if ord(char) >= 0x2E80)
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# --- select_history_window ---


def test_all_turns_within_budget_returned_oldest_first():
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = _select(messages, budget=1000)

    assert _contents(window) == ["q1", "a1", "q2", "a2"]
    assert [message.role for message in window] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]


def test_budget_exhaustion_drops_oldest_turns_whole():
    # Each turn costs 4 chars (2 + 2).
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = _select(messages, budget=4)

    assert _contents(window) == ["q2", "a2"]


def test_no_orphan_half_turn_is_ever_included():
    # Budget 7 fits the newest turn (4) but not the older one (4 more) — the
    # walk stops at the turn boundary instead of including its user half.
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = _select(messages, budget=7)

    assert _contents(window) == ["q2", "a2"]


def test_newest_turn_over_budget_means_no_history():
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = _select(messages, budget=3)

    assert window == []


def test_turn_cost_exactly_equal_to_budget_is_included():
    messages = _newest_first(("q1", "a1"))

    window = _select(messages, budget=4)

    assert _contents(window) == ["q1", "a1"]


def test_zero_budget_returns_no_history():
    window = _select(_newest_first(("q1", "a1")), budget=0)

    assert window == []


def test_empty_input_returns_empty_window():
    assert _select([], budget=1000) == []


def test_trailing_unpaired_user_message_is_not_a_turn():
    # A failed run leaves an unanswered question; it must neither enter the
    # window alone nor consume budget.
    messages = _newest_first(("q1", "a1"), trailing_user="unanswered")

    window = _select(messages, budget=4)

    assert _contents(window) == ["q1", "a1"]


def test_only_an_unpaired_user_message_returns_no_history():
    window = _select([_msg(MessageRole.USER, "q1")], budget=1000)

    assert window == []


def test_malformed_sequence_stops_the_walk():
    # Two users in a row (corrupt history): nothing after the break pairs
    # cleanly, so only turns found before it qualify.
    messages = [
        _msg(MessageRole.USER, "u2"),
        _msg(MessageRole.USER, "u1"),
        _msg(MessageRole.ASSISTANT, "a0"),
    ]

    window = _select(messages, budget=1000)

    assert window == []


# --- token measure + long-document guardrail ---


def test_char_fitting_session_drops_oldest_turn_under_token_measure():
    # AC1: both turns fit a 50-char budget, but under the token measure the
    # CJK-heavy newest turn costs 13 tokens and the older English turn 10 —
    # a 20-token budget keeps only the newest turn, where the char budget
    # kept both. The injected measure, not len(), decides.
    messages = _newest_first(
        ("what are the pros", "several pros here"),
        ("那它的缺点呢", "主要缺点有三点"),
    )

    char_window = _select(messages, budget=50)
    token_window = select_history_window(
        messages, budget=20, measure=_cjk_aware_measure, per_turn_cap=0
    )

    assert len("".join(_contents(char_window))) <= 50
    assert len(char_window) == 4
    assert _contents(token_window) == ["那它的缺点呢", "主要缺点有三点"]


def test_oversized_turn_is_bounded_so_an_older_turn_survives():
    # AC2: the newest turn alone exceeds the per-turn cap; it is admitted
    # truncated-with-marker so an older fitting turn keeps its budget share —
    # no silent single-turn collapse.
    messages = _newest_first(("old question", "old answer"), ("q", "x" * 500))

    window = select_history_window(messages, budget=400, measure=len, per_turn_cap=200)

    contents = _contents(window)
    assert contents[:2] == ["old question", "old answer"]  # older turn survives
    assert contents[2] == "q"  # the short side of the oversized turn stands whole
    bounded = contents[3]
    assert bounded.endswith(TRUNCATION_MARKER)
    assert bounded.startswith("x")  # a prefix, not the whole document
    assert len(bounded) < 500


def test_bounding_never_mutates_the_input_rows():
    # The bounded carriers are detached copies; the rows read from the DB
    # (the persisted record) keep their full content after selection.
    messages = _newest_first(("q", "x" * 500))

    window = select_history_window(messages, budget=400, measure=len, per_turn_cap=200)

    assert messages[0].content == "x" * 500  # untouched ORM row
    assert messages[1].content == "q"
    assert window[-1] is not messages[0]
    assert window[-1].content.endswith(TRUNCATION_MARKER)


def test_bounded_turn_still_over_budget_means_no_history():
    # Even bounded, a turn that does not fit the remaining budget stops the
    # walk: the question stands alone (never a silent partial window).
    messages = _newest_first(("q", "x" * 500))

    window = select_history_window(messages, budget=100, measure=len, per_turn_cap=200)

    assert window == []


def test_per_turn_cap_zero_disables_the_guardrail():
    # `per_turn_cap <= 0` (fraction >= 1.0 in Settings): the old
    # all-or-nothing walk — an oversized turn stops it whole, unbounded.
    messages = _newest_first(("old question", "old answer"), ("q", "x" * 500))

    window = _select(messages, budget=400)

    assert window == []


def test_both_sides_oversized_stays_within_the_cap():
    # A turn where user AND assistant exceed the cap: both copies are
    # bounded, each carrying the marker, and the bounded cost measures at or
    # under the cap with the same injected measure.
    messages = _newest_first(("y" * 500, "x" * 500))

    window = select_history_window(messages, budget=400, measure=len, per_turn_cap=300)

    contents = _contents(window)
    assert contents[0].endswith(TRUNCATION_MARKER)
    assert contents[1].endswith(TRUNCATION_MARKER)
    assert len(contents[0]) + len(contents[1]) <= 300
    assert len(contents[1]) > len(contents[0])  # the costlier side keeps less


# --- to_message_history ---


def test_to_message_history_maps_roles_onto_model_messages():
    history = to_message_history([_msg(MessageRole.USER, "q1"), _msg(MessageRole.ASSISTANT, "a1")])

    assert len(history) == 2
    request = history[0]
    response = history[1]
    assert isinstance(request, ModelRequest)
    assert isinstance(request.parts[0], UserPromptPart)
    assert request.parts[0].content == "q1"
    assert isinstance(response, ModelResponse)
    assert isinstance(response.parts[0], TextPart)
    assert response.parts[0].content == "a1"


def test_to_message_history_empty_is_empty():
    assert to_message_history([]) == []


# --- derive_title ---


def test_short_question_becomes_the_title_verbatim():
    assert derive_title("What is a zorblat?") == "What is a zorblat?"


def test_multiline_question_collapses_to_one_line():
    assert derive_title("What about\n  zorblat   exactly?") == "What about zorblat exactly?"


def test_long_question_truncates_to_the_maximum_length():
    question = "q" * 200

    title = derive_title(question)

    assert len(title) == 80
    assert title == "q" * 79 + "…"


def test_truncation_happens_at_a_word_boundary_where_possible():
    title = derive_title("word " * 30)  # 150 chars, spaces every 5th

    assert len(title) == 80
    assert not title.endswith(" ")  # rstrip before the ellipsis


def test_whitespace_only_question_falls_back_to_default_title():
    assert derive_title("   \n\t ") == "New chat"
