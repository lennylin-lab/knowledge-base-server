"""Stream bridge units: arg parsing, failure classification, drain discipline.

The integration behavior (order inside a live stream) is covered by the
service and API tests; this module pins the bridge's edge decisions that a
scripted FunctionModel run cannot reach: malformed model args, the retry
prompt (framework-level) failure shape, and the MCP error-string shape.
"""

from __future__ import annotations

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)

from app.schemas.chat import ToolCallStartedEvent
from app.services.stream_bridge import RunEventBridge, _is_tool_failure, _parse_tool_args


def test_parse_tool_args_json_string_decodes():
    assert _parse_tool_args(ToolCallPart(tool_name="t", args='{"query": "x"}')) == {"query": "x"}


def test_parse_tool_args_dict_passes_through():
    assert _parse_tool_args(ToolCallPart(tool_name="t", args={"a": 1})) == {"a": 1}


def test_parse_tool_args_malformed_payloads_degrade_to_empty_dict():
    # Display-only progress data: a broken payload must never break the stream.
    assert _parse_tool_args(ToolCallPart(tool_name="t", args="{not json")) == {}
    assert _parse_tool_args(ToolCallPart(tool_name="t", args='["list"]')) == {}
    assert _parse_tool_args(ToolCallPart(tool_name="t", args=None)) == {}


def test_retry_prompt_result_maps_to_failed_status():
    result = FunctionToolResultEvent(
        part=RetryPromptPart(tool_name="search_knowledge", content="Validation failed")
    )
    assert _is_tool_failure(result) is True


def test_mcp_error_string_result_maps_to_failed_status():
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="mcp_alpha_add",
            content="tool mcp_alpha_add failed: RuntimeError",
            tool_call_id="call_2",
        )
    )
    assert _is_tool_failure(result) is True


def test_successful_result_maps_to_success_status():
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="search_knowledge", content="[1] Notes", tool_call_id="call_3"
        )
    )
    assert _is_tool_failure(result) is False


async def test_handler_buffers_started_events_and_limit_is_injected():
    """The handler records started events with the deps `limit` injected."""

    class _Deps:
        limit = 8

    bridge = RunEventBridge()
    call = FunctionToolCallEvent(
        part=ToolCallPart(tool_name="search_knowledge", args='{"query": "zorblat"}')
    )

    async def stream():
        yield call

    class _Ctx:
        deps = _Deps()

    await bridge.on_agent_event(_Ctx(), stream())  # type: ignore[arg-type]

    assert bridge.drain_started() == [
        ToolCallStartedEvent(
            call_id=call.tool_call_id,
            tool_name="search_knowledge",
            args={"query": "zorblat", "limit": 8},
        )
    ]
    # Draining leaves the buffers empty.
    assert bridge.drain_started() == []
    assert bridge.drain_finished() == []


async def test_handler_ignores_non_tool_events():
    from pydantic_ai.messages import PartStartEvent, TextPart

    bridge = RunEventBridge()

    async def stream():
        yield PartStartEvent(index=0, part=TextPart(content="answer"))

    await bridge.on_agent_event(None, stream())  # type: ignore[arg-type]

    assert bridge.drain_started() == []
    assert bridge.drain_finished() == []
