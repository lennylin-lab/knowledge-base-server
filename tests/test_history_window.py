"""Pure chat-session helpers (offline): history-window selection, title
derivation, message-history rebuilding — no DB, no LLM, no infrastructure."""

from __future__ import annotations

from uuid import uuid4

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from app.models.chat import ChatMessage, MessageRole
from app.services.chat import select_history_window, to_message_history
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


# --- select_history_window ---


def test_all_turns_within_budget_returned_oldest_first():
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = select_history_window(messages, budget=1000)

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

    window = select_history_window(messages, budget=4)

    assert _contents(window) == ["q2", "a2"]


def test_no_orphan_half_turn_is_ever_included():
    # Budget 7 fits the newest turn (4) but not the older one (4 more) — the
    # walk stops at the turn boundary instead of including its user half.
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = select_history_window(messages, budget=7)

    assert _contents(window) == ["q2", "a2"]


def test_newest_turn_over_budget_means_no_history():
    messages = _newest_first(("q1", "a1"), ("q2", "a2"))

    window = select_history_window(messages, budget=3)

    assert window == []


def test_turn_cost_exactly_equal_to_budget_is_included():
    messages = _newest_first(("q1", "a1"))

    window = select_history_window(messages, budget=4)

    assert _contents(window) == ["q1", "a1"]


def test_zero_budget_returns_no_history():
    window = select_history_window(_newest_first(("q1", "a1")), budget=0)

    assert window == []


def test_empty_input_returns_empty_window():
    assert select_history_window([], budget=1000) == []


def test_trailing_unpaired_user_message_is_not_a_turn():
    # A failed run leaves an unanswered question; it must neither enter the
    # window alone nor consume budget.
    messages = _newest_first(("q1", "a1"), trailing_user="unanswered")

    window = select_history_window(messages, budget=4)

    assert _contents(window) == ["q1", "a1"]


def test_only_an_unpaired_user_message_returns_no_history():
    window = select_history_window([_msg(MessageRole.USER, "q1")], budget=1000)

    assert window == []


def test_malformed_sequence_stops_the_walk():
    # Two users in a row (corrupt history): nothing after the break pairs
    # cleanly, so only turns found before it qualify.
    messages = [
        _msg(MessageRole.USER, "u2"),
        _msg(MessageRole.USER, "u1"),
        _msg(MessageRole.ASSISTANT, "a0"),
    ]

    window = select_history_window(messages, budget=1000)

    assert window == []


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
