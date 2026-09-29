"""Gateway error mapper: code table, tolerant envelope parsing, Retry-After.

Fixtures carry realistic gateway envelope bodies (issue #5); the SDK path is
exercised with stubbed httpx responses exactly like the embedding provider
tests (quality-guidelines: provider shapes are faked at the llm/ boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from structlog.testing import capture_logs

from app.core.exceptions import (
    AppError,
    CapabilityNotSupportedError,
    EmbeddingDimMismatchError,
    GatewayInvalidRequestError,
    GatewayUpstreamRejectedError,
    LLMGatewayAuthFailedError,
    LLMProviderError,
    LLMRateLimitedError,
    ModelNotAllowedError,
    SearchIndexError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.llm.gateway_errors import map_provider_error

GATEWAY_MESSAGE = "gateway secret detail: upstream said forbidden things"


def _http_error(
    status_code: int,
    body: object,
    *,
    headers: dict[str, str] | None = None,
) -> ModelHTTPError:
    return ModelHTTPError(status_code=status_code, model_name="gateway", body=body, headers=headers)


def _envelope(
    code: str | None = None,
    *,
    request_id: str | None = None,
    message: str = GATEWAY_MESSAGE,
) -> dict[str, object]:
    error: dict[str, object] = {"message": message}
    if code is not None:
        error["code"] = code
    if request_id is not None:
        error["request_id"] = request_id
    return {"error": error}


# --- per-code matrix (AC1): every documented gateway error.code ---


@pytest.mark.parametrize(
    ("gateway_code", "gateway_status", "expected_type", "expected_code", "expected_status"),
    [
        ("invalid_request", 400, GatewayInvalidRequestError, "gateway_invalid_request", 422),
        (
            "schema_validation_failed",
            400,
            GatewayInvalidRequestError,
            "gateway_invalid_request",
            422,
        ),
        (
            "invalid_tool_arguments",
            400,
            GatewayInvalidRequestError,
            "gateway_invalid_request",
            422,
        ),
        (
            "capability_not_supported",
            400,
            CapabilityNotSupportedError,
            "capability_not_supported",
            502,
        ),
        (
            "upstream_rejected_request",
            400,
            GatewayUpstreamRejectedError,
            "gateway_upstream_rejected",
            502,
        ),
        ("invalid_api_key", 401, LLMGatewayAuthFailedError, "llm_gateway_auth_failed", 503),
        ("api_key_expired", 401, LLMGatewayAuthFailedError, "llm_gateway_auth_failed", 503),
        ("api_key_revoked", 401, LLMGatewayAuthFailedError, "llm_gateway_auth_failed", 503),
        ("model_not_allowed", 403, ModelNotAllowedError, "model_not_allowed", 403),
        ("rate_limit_exceeded", 429, LLMRateLimitedError, "rate_limited", 429),
        ("quota_exceeded", 429, LLMRateLimitedError, "rate_limited", 429),
        ("upstream_unavailable", 503, UpstreamUnavailableError, "upstream_unavailable", 503),
        ("no_route_available", 503, UpstreamUnavailableError, "upstream_unavailable", 503),
        ("limiter_unavailable", 503, UpstreamUnavailableError, "upstream_unavailable", 503),
        ("upstream_timeout", 504, UpstreamTimeoutError, "upstream_timeout", 504),
        (
            "embedding_dim_mismatch",
            500,
            EmbeddingDimMismatchError,
            "embedding_dim_mismatch",
            502,
        ),
    ],
)
def test_gateway_code_maps_to_dedicated_error(
    gateway_code: str,
    gateway_status: int,
    expected_type: type[AppError],
    expected_code: str,
    expected_status: int,
) -> None:
    failure = map_provider_error(_http_error(gateway_status, _envelope(gateway_code)))

    assert type(failure) is expected_type
    assert failure.code == expected_code
    assert failure.status_code == expected_status  # never the gateway's status
    assert failure.details["gateway_code"] == gateway_code
    # The gateway's own message never reaches a client (AC5).
    assert GATEWAY_MESSAGE not in failure.message


def test_upstream_rate_limited_maps_to_rate_limited_despite_503() -> None:
    """Semantically 429: the caller should back off, so it maps to the
    rate-limited class even though the gateway delivers it on a 503."""
    failure = map_provider_error(_http_error(503, _envelope("upstream_rate_limited")))

    assert type(failure) is LLMRateLimitedError
    assert failure.status_code == 429


# --- details (M2): gateway_code / gateway_request_id / quota reason ---


def test_request_id_present_rides_in_details() -> None:
    failure = map_provider_error(
        _http_error(503, _envelope("invalid_api_key", request_id="gw-req-42"))
    )

    assert failure.details == {"gateway_code": "invalid_api_key", "gateway_request_id": "gw-req-42"}


def test_request_id_absent_is_omitted_from_details() -> None:
    failure = map_provider_error(_http_error(503, _envelope("invalid_api_key")))

    assert failure.details == {"gateway_code": "invalid_api_key"}
    assert "gateway_request_id" not in failure.details


def test_quota_exceeded_is_flagged_with_reason_quota() -> None:
    failure = map_provider_error(_http_error(429, _envelope("quota_exceeded")))

    assert type(failure) is LLMRateLimitedError
    assert failure.details["reason"] == "quota"


def test_plain_rate_limit_carries_no_reason() -> None:
    failure = map_provider_error(_http_error(429, _envelope("rate_limit_exceeded")))

    assert "reason" not in failure.details


# --- Retry-After threading (M2, AC4) ---


def test_retry_after_header_parsed_on_rate_limit() -> None:
    failure = map_provider_error(
        _http_error(429, _envelope("rate_limit_exceeded"), headers={"Retry-After": "7"})
    )

    assert failure.retry_after_seconds == 7.0


def test_retry_after_absent_leaves_attribute_unset() -> None:
    failure = map_provider_error(_http_error(429, _envelope("rate_limit_exceeded")))

    assert failure.retry_after_seconds is None


def test_retry_after_unparseable_leaves_attribute_unset() -> None:
    failure = map_provider_error(
        _http_error(429, _envelope("rate_limit_exceeded"), headers={"Retry-After": "soon"})
    )

    assert failure.retry_after_seconds is None


def test_retry_after_http_date_form_parses() -> None:
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    failure = map_provider_error(
        _http_error(429, _envelope("rate_limit_exceeded"), headers={"Retry-After": soon})
    )

    assert failure.retry_after_seconds is not None
    assert 0.0 <= failure.retry_after_seconds <= 30.0


def test_upstream_rate_limited_honors_retry_after_on_its_503() -> None:
    failure = map_provider_error(
        _http_error(503, _envelope("upstream_rate_limited"), headers={"Retry-After": "12"})
    )

    assert type(failure) is LLMRateLimitedError
    assert failure.retry_after_seconds == 12.0


def test_non_429_failures_never_carry_retry_after() -> None:
    failure = map_provider_error(
        _http_error(503, _envelope("upstream_unavailable"), headers={"Retry-After": "9"})
    )

    assert failure.retry_after_seconds is None


# --- legacy fallbacks: byte-identical to the pre-gateway mapping ---


def test_unknown_code_falls_back_to_provider_error_without_details() -> None:
    failure = map_provider_error(_http_error(503, _envelope("brand_new_code")))

    assert type(failure) is LLMProviderError
    assert failure.message == "LLM provider request failed"
    assert failure.details == {}


def test_unknown_code_on_429_still_falls_back_to_rate_limited() -> None:
    failure = map_provider_error(_http_error(429, _envelope("brand_new_code")))

    assert type(failure) is LLMRateLimitedError
    assert failure.message == "LLM provider rate limit exceeded"


def test_non_dict_body_falls_back() -> None:
    for body in ("plain text", None, ["list"]):
        failure = map_provider_error(_http_error(500, body))
        assert type(failure) is LLMProviderError


def test_missing_error_key_falls_back() -> None:
    failure = map_provider_error(_http_error(503, {"message": "upstream exploded"}))

    assert type(failure) is LLMProviderError


def test_non_string_code_or_request_id_degrades_tolerantly() -> None:
    body = {"error": {"code": 123, "request_id": ["x"]}}
    failure = map_provider_error(_http_error(500, body))

    assert type(failure) is LLMProviderError
    assert failure.details == {}


# --- transport and unknown failures ---


def test_model_api_error_maps_to_provider_error() -> None:
    failure = map_provider_error(ModelAPIError(model_name="m", message="connection died"))

    assert type(failure) is LLMProviderError
    assert failure.message == "LLM provider request failed"


def test_plain_exception_maps_to_internal_error() -> None:
    failure = map_provider_error(RuntimeError("bug"))

    assert type(failure) is AppError
    assert failure.code == "internal_error"
    assert failure.status_code == 500


def test_app_error_passthrough_is_idempotent() -> None:
    original = SearchIndexError("es down", details={"operation": "search"})

    assert map_provider_error(original) is original


# --- SDK path (llm/embeddings.py route): stubbed httpx response ---


def _sdk_status_error(
    exc_type: type[openai.APIStatusError],
    status: int,
    body: object,
    *,
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://provider.test/v1/embeddings")
    response = httpx.Response(status, headers=headers, request=request)
    return exc_type("sdk message", response=response, body=body)


def test_sdk_rate_limit_with_gateway_quota_body_maps_and_carries_retry_after() -> None:
    failure = map_provider_error(
        _sdk_status_error(
            openai.RateLimitError,
            429,
            _envelope("quota_exceeded", request_id="sdk-1"),
            headers={"retry-after": "21"},
        )
    )

    assert type(failure) is LLMRateLimitedError
    assert failure.details["gateway_code"] == "quota_exceeded"
    assert failure.details["gateway_request_id"] == "sdk-1"
    assert failure.details["reason"] == "quota"
    assert failure.retry_after_seconds == 21.0


def test_sdk_rate_limit_without_gateway_body_falls_back_with_retry_after() -> None:
    failure = map_provider_error(
        _sdk_status_error(openai.RateLimitError, 429, None, headers={"retry-after": "8"})
    )

    assert type(failure) is LLMRateLimitedError
    assert failure.details == {}
    assert failure.retry_after_seconds == 8.0


def test_sdk_status_error_with_gateway_code_maps() -> None:
    failure = map_provider_error(
        _sdk_status_error(openai.APIStatusError, 403, _envelope("model_not_allowed"))
    )

    assert type(failure) is ModelNotAllowedError
    assert failure.status_code == 403


def test_sdk_connection_error_maps_to_provider_error() -> None:
    failure = map_provider_error(
        openai.APIConnectionError(request=httpx.Request("POST", "http://provider.test"))
    )

    assert type(failure) is LLMProviderError


# --- structured log (R7): ids only, never gateway text ---


def test_mapped_failure_logs_ids_only() -> None:
    with capture_logs() as logs:
        map_provider_error(
            _http_error(503, _envelope("invalid_api_key", request_id="gw-9"), headers={"x": "y"})
        )

    mapped = [entry for entry in logs if entry["event"] == "gateway_error_mapped"]
    assert len(mapped) == 1
    entry = mapped[0]
    assert entry["gateway_code"] == "invalid_api_key"
    assert entry["gateway_request_id"] == "gw-9"
    assert entry["status_code"] == 503
    # Gateway message text and upstream content never reach the logs.
    assert GATEWAY_MESSAGE not in str(entry)
    assert all(GATEWAY_MESSAGE not in str(log_entry) for log_entry in logs)
