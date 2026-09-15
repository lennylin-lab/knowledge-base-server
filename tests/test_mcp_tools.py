"""MCP tool wrapping (offline): naming, schema passthrough, failure policy.

The manager is a fake recording calls; the agent run is a scripted
FunctionModel, so these tests pin the model-visible contract (name,
description, the server's own JSON schema) and the degrade-don't-abort
policy without any infrastructure.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolDefinition
from structlog.testing import capture_logs

from app.agents.qa import ChatDeps, SourceCollector, build_qa_agent
from app.mcp.manager import McpToolInfo, McpToolResult
from app.mcp.tools import _DESCRIPTION_LIMIT, build_agent_tools
from app.models.tenant import DEFAULT_TENANT_ID
from fakes import FakeMcpManager, StubRetriever, _tool_return_count

ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
    "required": ["a", "b"],
}


def _info(
    server: str = "alpha", name: str = "add", description: str = "Add two integers."
) -> McpToolInfo:
    return McpToolInfo(
        server=server, name=name, description=description, input_schema=dict(ADD_SCHEMA)
    )


def test_names_carry_the_server_prefix() -> None:
    tools = build_agent_tools(FakeMcpManager(), [_info(server="alpha", name="add")])

    assert len(tools) == 1
    assert tools[0].name == "mcp_alpha_add"
    assert tools[0].description == "Add two integers."


def test_empty_description_falls_back_to_a_generated_one() -> None:
    tools = build_agent_tools(FakeMcpManager(), [_info(description="")])

    assert tools[0].description == "MCP tool 'add' on server 'alpha'."


def test_long_descriptions_are_truncated() -> None:
    tools = build_agent_tools(FakeMcpManager(), [_info(description="x" * 5000)])

    assert tools[0].description is not None
    assert len(tools[0].description) == _DESCRIPTION_LIMIT


def test_duplicate_composed_names_get_numeric_suffixes() -> None:
    # "mcp_a_b_c" is reachable from (server a, tool b_c) and (server a_b,
    # tool c): the second registration must not shadow the first.
    tools = build_agent_tools(
        FakeMcpManager(),
        [_info(server="a", name="b_c"), _info(server="a_b", name="c")],
    )

    assert [tool.name for tool in tools] == ["mcp_a_b_c", "mcp_a_b_c_2"]


def _probe_model(
    tool_name: str, arguments: dict[str, Any], seen_defs: list[ToolDefinition]
) -> FunctionModel:
    """Scripted model: capture the tool definitions, call the tool, answer."""

    async def stream_function(messages: list[ModelMessage], info: object) -> Any:
        if not seen_defs:
            for tool_def in info.function_tools:
                seen_defs.append(tool_def)
        if _tool_return_count(messages) == 0:
            yield {
                0: DeltaToolCall(
                    name=tool_name,
                    json_args=json.dumps(arguments),
                    tool_call_id="call_0",
                )
            }
            return
        yield "answer after tools"

    return FunctionModel(stream_function=stream_function, model_name="probe")


async def _run_agent(model: FunctionModel, tools: list[Any]) -> tuple[str, SourceCollector]:
    collector = SourceCollector()
    agent = build_qa_agent(model, extra_tools=tools)
    async with agent.run_stream(
        "question",
        deps=ChatDeps(
            retriever=StubRetriever(), limit=8, collector=collector, tenant_id=DEFAULT_TENANT_ID
        ),
    ) as result:
        output = await result.get_output()
    return output, collector


async def test_model_sees_the_servers_own_schema_and_wrapper_round_trips() -> None:
    manager = FakeMcpManager(result=McpToolResult(text="3", structured={"result": 3}))
    tools = build_agent_tools(manager, [_info()])
    seen: list[ToolDefinition] = []

    output, collector = await _run_agent(
        _probe_model("mcp_alpha_add", {"a": 1, "b": 2}, seen), tools
    )

    assert output == "answer after tools"
    # The definition swap (`prepare`) is transparent: same name, the server's
    # schema and description verbatim. (`search_knowledge` registers too —
    # the wrapped tool appends after it, never replaces it.)
    assert [tool_def.name for tool_def in seen] == ["search_knowledge", "mcp_alpha_add"]
    swapped = seen[1]
    assert swapped.parameters_json_schema == ADD_SCHEMA
    assert swapped.description == "Add two integers."
    # Arguments flowed through the generic wrapper to the manager.
    assert manager.calls == [("alpha", "add", {"a": 1, "b": 2})]
    # The external call landed in the run's tool_calls total.
    assert collector.tool_calls == 1


async def test_structured_only_result_is_returned_as_json() -> None:
    manager = FakeMcpManager(result=McpToolResult(text="", structured={"result": 7}))
    tools = build_agent_tools(manager, [_info()])
    tool_returns: list[str] = []

    async def stream_function(messages: list[ModelMessage], info: object) -> Any:
        fresh = [
            str(part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "mcp_alpha_add"
        ]
        tool_returns.extend(fresh[len(tool_returns) :])
        if _tool_return_count(messages) == 0:
            yield {
                0: DeltaToolCall(
                    name="mcp_alpha_add",
                    json_args=json.dumps({"a": 3, "b": 4}),
                    tool_call_id="call_0",
                )
            }
            return
        yield "answer"

    with capture_logs() as logs:
        await _run_agent(FunctionModel(stream_function=stream_function, model_name="probe"), tools)

    # With no text blocks, the model sees the serialized structured content.
    assert tool_returns == ['{"result": 7}']
    called = next(entry for entry in logs if entry["event"] == "mcp_tool_called")
    assert called["tool"] == "add"
    assert called["server"] == "alpha"
    assert called["latency_ms"] >= 0


async def test_failing_tool_returns_error_string_and_never_raises() -> None:
    manager = FakeMcpManager(error=RuntimeError("transport exploded"))
    tools = build_agent_tools(manager, [_info()])
    tool_returns: list[str] = []

    async def stream_function(messages: list[ModelMessage], info: object) -> Any:
        fresh = [
            str(part.content)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "mcp_alpha_add"
        ]
        tool_returns.extend(fresh[len(tool_returns) :])
        if _tool_return_count(messages) == 0:
            yield {
                0: DeltaToolCall(
                    name="mcp_alpha_add",
                    json_args=json.dumps({"a": 1, "b": 2}),
                    tool_call_id="call_0",
                )
            }
            return
        yield "answer despite failure"

    collector = SourceCollector()
    agent = build_qa_agent(
        FunctionModel(stream_function=stream_function, model_name="probe"),
        extra_tools=tools,
    )
    with capture_logs() as logs:
        async with agent.run_stream(
            "question",
            deps=ChatDeps(
                retriever=StubRetriever(), limit=8, collector=collector, tenant_id=DEFAULT_TENANT_ID
            ),
        ) as result:
            output = await result.get_output()

    # The run completed; the model saw a concise error string, not an exception.
    assert output == "answer despite failure"
    assert tool_returns == ["tool mcp_alpha_add failed: RuntimeError"]
    failed = next(entry for entry in logs if entry["event"] == "mcp_tool_failed")
    assert failed["tool"] == "add"
    assert failed["server"] == "alpha"
    assert failed["error_class"] == "RuntimeError"
    # The attempted call still counts.
    assert collector.tool_calls == 1
