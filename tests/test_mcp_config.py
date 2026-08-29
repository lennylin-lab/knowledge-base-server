"""MCP config parsing (offline): shape, transport validation, error loudness.

`mcp.json` itself is gitignored user config (may hold real keys in `env`);
these tests exercise `load_mcp_config` against temp files and the committed
`mcp.json.example`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.core.exceptions import ValidationError
from app.mcp.manager import McpServerConfig, load_mcp_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_config(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_valid_stdio_and_http_entries_parse(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {
            "mcpServers": {
                "context7": {
                    "command": "npx",
                    "args": ["-y", "@upstash/context7-mcp"],
                    "env": {"API_TOKEN": "secret-value"},
                },
                "websearch": {"url": "https://mcp.example/search/mcp"},
            }
        },
    )

    servers = load_mcp_config(path)

    assert set(servers) == {"context7", "websearch"}
    context7 = servers["context7"]
    assert isinstance(context7, McpServerConfig)
    assert context7.command == "npx"
    assert context7.args == ["-y", "@upstash/context7-mcp"]
    assert context7.env == {"API_TOKEN": "secret-value"}
    assert context7.url is None
    assert context7.transport == "stdio"
    websearch = servers["websearch"]
    assert websearch.url == "https://mcp.example/search/mcp"
    assert websearch.command is None
    assert websearch.args == []
    assert websearch.transport == "http"


def test_entry_with_both_transports_is_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"mcpServers": {"broken": {"command": "npx", "url": "https://mcp.example/mcp"}}},
    )

    with pytest.raises(ValidationError) as excinfo:
        load_mcp_config(path)

    assert "exactly one" in str(excinfo.value.details["errors"][0]["msg"])


def test_entry_with_neither_transport_is_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"mcpServers": {"broken": {"args": ["--flag"]}}})

    with pytest.raises(ValidationError):
        load_mcp_config(path)


def test_missing_file_is_an_empty_config_with_info_log(tmp_path: Path) -> None:
    with capture_logs() as logs:
        servers = load_mcp_config(tmp_path / "absent.json")

    assert servers == {}
    assert any(entry["event"] == "mcp_config_missing" for entry in logs)


def test_malformed_json_is_a_loud_validation_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "{not json at all")

    with pytest.raises(ValidationError) as excinfo:
        load_mcp_config(path)

    assert excinfo.value.details["path"] == str(path)
    assert "line" in excinfo.value.details


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"mcpServers": {}, "mcpServerz": {}, "extra": 1})

    with pytest.raises(ValidationError):
        load_mcp_config(path)


def test_env_values_never_leak_into_error_details(tmp_path: Path) -> None:
    # A failing entry alongside real env values: pydantic's raw errors echo
    # inputs, so the scrub must drop them before the details reach logs.
    path = _write_config(
        tmp_path,
        {
            "mcpServers": {
                "leaky": {
                    "command": "npx",
                    "url": "https://mcp.example/mcp",
                    "env": {"API_TOKEN": "super-secret-value"},
                }
            }
        },
    )

    with pytest.raises(ValidationError) as excinfo:
        load_mcp_config(path)

    assert "super-secret-value" not in str(excinfo.value.details)
    assert all("super-secret-value" not in str(error) for error in excinfo.value.details["errors"])


def test_empty_servers_root_parses_to_empty_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"mcpServers": {}})

    assert load_mcp_config(path) == {}


def test_committed_example_file_parses() -> None:
    servers = load_mcp_config(REPO_ROOT / "mcp.json.example")

    assert set(servers) == {"context7", "websearch"}
    assert servers["context7"].command == "npx"
    assert servers["websearch"].url == "https://mcp.example/search/mcp"
