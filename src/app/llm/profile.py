"""Gateway `retrieval_profile` override layer (server-side validation).

The profile is opaque to the gateway; validation lives here. Keys map
1:1 onto the `SEARCH_*` Settings fields (snake_case), applied at the single
retriever choke point (`api/deps.py::_build_retriever`). Precedence:
profile > env kwargs > `Retriever` module defaults.
"""

from __future__ import annotations

from collections.abc import Mapping

import structlog

logger = structlog.get_logger(__name__)

# Explicit allowlist: retrieval_profile key -> retriever kwarg, with the
# expected value type. Unknown keys are ignored (debug log); invalid values
# are tolerated with a warning (the env value is kept).
_PROFILE_KEY_MAP: Mapping[str, tuple[str, type]] = {
    "search_bm25_min_score": ("bm25_min_score", float),
    "search_bm25_min_coverage": ("bm25_min_coverage", str),
    "search_vector_max_distance": ("vector_max_distance", float),
    "search_vector_rescue_margin": ("vector_rescue_margin", float),
    "search_vector_rescue_max_distance": ("vector_rescue_max_distance", float),
    "search_vector_rescue_trigger_max_distance": (
        "vector_rescue_trigger_max_distance",
        float,
    ),
    "search_rrf_min_relative": ("rrf_min_relative", float),
    "search_max_query_length": ("max_query_length", int),
}

# Retriever kwargs the profile may override (the per-threshold source map in
# the startup resolution log covers exactly these).
RETRIEVER_THRESHOLD_KEYS: tuple[str, ...] = tuple(
    sorted(kwarg for kwarg, _ in _PROFILE_KEY_MAP.values())
)

_Value = int | float | str


def _coerce(value: object, expected: type) -> _Value | None:
    """Cast a profile value to `expected`, or None when unusable.

    Floats accept int/float and numeric strings; ints accept non-bool ints
    (and integer-valued floats); strings pass through as-is. Booleans are
    never valid threshold values.
    """
    if isinstance(value, bool):
        return None
    if expected is str:
        return value if isinstance(value, str) else None
    if expected is float:
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None
    if expected is int:
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None
    return None


def apply_profile(
    base_kwargs: Mapping[str, _Value],
    profile: Mapping[str, object],
) -> dict[str, _Value]:
    """Overlay the profile onto the env-derived retriever kwargs.

    Pure w.r.t. inputs (logging aside): unknown profile keys are ignored with
    a debug log; a value that fails validation keeps the base (env) value and
    warns — the retriever is always constructible.
    """
    merged: dict[str, _Value] = dict(base_kwargs)
    for key, raw in profile.items():
        mapped = _PROFILE_KEY_MAP.get(key)
        if mapped is None:
            logger.debug("profile_key_ignored", key=key)
            continue
        kwarg, expected = mapped
        value = _coerce(raw, expected)
        if value is None:
            logger.warning(
                "profile_value_invalid",
                key=key,
                value_type=type(raw).__name__,
                expected=expected.__name__,
            )
            continue
        merged[kwarg] = value
    return merged
