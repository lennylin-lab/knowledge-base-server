"""`build_token_counter`: model-to-encoding wiring and offline fallbacks.

tiktoken is stubbed at the module boundary (monkeypatched) so the suite
never loads BPE data — no test needs the network (quality-guidelines:
offline by default). The heuristic fallback path is asserted directly
against its deterministic definition.
"""

from __future__ import annotations

import tiktoken

from app.llm.tokens import build_token_counter, heuristic_token_count


class _StubEncoding:
    """Duck-typed stand-in for `tiktoken.Encoding` with scripted counts."""

    def __init__(self, counts: dict[str, int] | None = None, raise_on: tuple[str, ...] = ()):
        self._counts = counts or {}
        self._raise_on = raise_on
        self.calls: list[tuple[str, object]] = []

    def encode(self, text: str, *, allowed_special=(), disallowed_special="all") -> list[int]:
        self.calls.append((text, disallowed_special))
        if text in self._raise_on:
            raise ValueError(f"special token not allowed: {text}")
        if text in self._counts:
            return list(range(self._counts[text]))
        return list(text.encode("utf-8"))


def test_known_model_counts_with_its_encoding(monkeypatch):
    stub = _StubEncoding(counts={"hello world": 3})
    seen_models: list[str] = []

    def fake_for_model(model_name: str):
        seen_models.append(model_name)
        return stub

    monkeypatch.setattr(tiktoken, "encoding_for_model", fake_for_model)

    counter = build_token_counter("gpt-4o-mini")

    assert counter("hello world") == 3
    assert seen_models == ["gpt-4o-mini"]
    # Special-token-looking text is counted as plain content, not an error.
    assert stub.calls == [("hello world", ())]


def test_unknown_model_falls_back_to_default_encoding(monkeypatch):
    stub = _StubEncoding(counts={"abc": 2})
    seen_encodings: list[str] = []

    def unknown(model_name: str):
        raise KeyError(f"unmapped model name: {model_name}")

    def fake_get_encoding(name: str):
        seen_encodings.append(name)
        return stub

    monkeypatch.setattr(tiktoken, "encoding_for_model", unknown)
    monkeypatch.setattr(tiktoken, "get_encoding", fake_get_encoding)

    counter = build_token_counter("some-nonopenai-model")

    assert counter("abc") == 2
    assert seen_encodings == ["o200k_base"]


def test_unloadable_encoding_degrades_to_the_heuristic(monkeypatch):
    # Offline first use / missing BPE data: every loader call fails, yet
    # building the counter and counting must both keep working (never raise).
    def broken(*args, **kwargs):
        raise RuntimeError("offline: BPE data unavailable")

    monkeypatch.setattr(tiktoken, "encoding_for_model", broken)
    monkeypatch.setattr(tiktoken, "get_encoding", broken)

    counter = build_token_counter("anything")

    assert counter is heuristic_token_count
    assert counter("") == 0
    assert counter("abcd") == 1
    assert counter("你好世界") == 4


def test_count_time_encode_failure_still_never_raises(monkeypatch):
    # Even a built encoder can fail at count time (e.g. unencodable input);
    # the counter degrades to the heuristic instead of raising.
    stub = _StubEncoding(raise_on=("\ud800surrogate",))
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda name: stub)

    counter = build_token_counter("gpt-4o-mini")

    assert counter("\ud800surrogate") == heuristic_token_count("\ud800surrogate")
    assert counter("safe text") > 0


def test_heuristic_counts_cjk_text_per_character():
    assert heuristic_token_count("") == 0
    assert heuristic_token_count("你好世界") == 4
    assert heuristic_token_count("。。。") == 3  # CJK punctuation, one each


def test_heuristic_counts_other_text_four_chars_per_token():
    assert heuristic_token_count("a") == 1
    assert heuristic_token_count("abcd") == 1
    assert heuristic_token_count("abcde") == 2
    assert heuristic_token_count("abcdefgh") == 2


def test_heuristic_mixed_script_is_deterministic():
    mixed = "你好 world"
    assert heuristic_token_count(mixed) == 4  # 2 CJK + ceil(6/4)
    assert heuristic_token_count(mixed) == heuristic_token_count(mixed)
