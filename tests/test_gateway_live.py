"""Opt-in live probes against the local knowledge-base-gateway.

These tests verify that the Gateway service-account path (non-streaming and
streaming chat completions against the public model) actually works with a
real Gateway service key. They are excluded from the default offline suite
(`live_gateway` marker) and additionally skip unless the operator provides:

- ``KB_GATEWAY_LIVE_URL``    Gateway public API root, e.g. http://127.0.0.1:8091/v1
- ``KB_GATEWAY_LIVE_KEY``    a Gateway service key (minted via the admin API)
- ``KB_GATEWAY_LIVE_MODEL``  the Gateway public model name (e.g. gateway-echo)

Values are read from the environment only and never printed, logged, or
asserted on directly — only shapes and statuses are. Timeout is bounded.
"""

from __future__ import annotations

import os

import httpx
import pytest

_TIMEOUT = httpx.Timeout(10.0)


def _live_config() -> tuple[str, str, str]:
    """Return (base_url, key, model) or skip when the live env is absent."""
    base_url = os.environ.get("KB_GATEWAY_LIVE_URL", "")
    key = os.environ.get("KB_GATEWAY_LIVE_KEY", "")
    model = os.environ.get("KB_GATEWAY_LIVE_MODEL", "")
    if not (base_url and key and model):
        pytest.skip("gateway live env not configured (KB_GATEWAY_LIVE_*)")
    return base_url, key, model


@pytest.mark.live_gateway
async def test_gateway_non_streaming_echo_completion() -> None:
    base_url, key, model = _live_config()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "live probe"}],
                "max_tokens": 32,
                "stream": False,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["finish_reason"] == "stop"
    # Fake provider contract: last user message echoed back.
    assert body["choices"][0]["message"]["content"].startswith("echo:")
    assert body["usage"]["total_tokens"] > 0
    assert resp.headers.get("x-request-id")


@pytest.mark.live_gateway
async def test_gateway_streaming_sse_terminates_with_done() -> None:
    base_url, key, model = _live_config()
    chunks = 0
    terminated = False
    async with (
        httpx.AsyncClient(timeout=_TIMEOUT) as client,
        client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "live probe"}],
                "max_tokens": 32,
                "stream": True,
            },
        ) as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line == "data: [DONE]":
                terminated = True
            elif line.startswith("data: {"):
                chunks += 1
    assert chunks > 0
    assert terminated, "stream must end with the terminal data: [DONE] frame"


@pytest.mark.live_gateway
async def test_gateway_rejects_invalid_key_with_stable_envelope() -> None:
    base_url, _key, model = _live_config()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": "Bearer kb-definitely-invalid"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "live probe"}],
            },
        )
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["request_id"]
