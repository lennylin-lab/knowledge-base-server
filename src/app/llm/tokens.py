"""Token counting for the chat history budget — provider plumbing only.

`build_token_counter` turns a chat-model name into a `text -> token count`
function. tiktoken downloads its BPE data on first use, so encoder
construction is defensive end to end: an unknown / non-OpenAI model name
falls back to a default encoding, and any failure to build one (offline
first use, missing BPE files) degrades to a deterministic char/CJK
heuristic. Nothing here ever raises — a mis-measured history budget is a
graceful degradation, never a crashed turn. The chosen encoder is built once
and closed over (per service, like the model/client).
"""

from __future__ import annotations

from collections.abc import Callable

import tiktoken

# Encoding for models tiktoken cannot map by name (non-OpenAI CHAT_MODEL
# values pointed at OpenAI-compatible relays): the current-generation OpenAI
# encoding keeps counts sane for the OpenAI-family traffic this server sends.
_FALLBACK_ENCODING = "o200k_base"

# Code points real tokenizers encode at roughly one token per character
# (CJK ideographs, kana, CJK punctuation, fullwidth forms). Everything else
# is estimated at the ~4 chars/token average of English prose.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),  # CJK Symbols and Punctuation
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and Fullwidth Forms
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def heuristic_token_count(text: str) -> int:
    """Deterministic offline token estimate; the fallback when no tiktoken
    encoding can be loaded (and what the fallback-path unit tests assert).

    CJK characters count as one token each (close to real tokenizers); the
    remaining text counts as one token per four characters, rounded up.
    Empty text is zero tokens; any non-empty text at least one.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _counter_from_encoder(encoder: tiktoken.Encoding) -> Callable[[str], int]:
    """Close over the built encoder; total against any count-time failure."""

    def count(text: str) -> int:
        try:
            # disallowed_special=(): pasted "<|endoftext|>"-style text is
            # ordinary content, never a tokenizer error.
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:
            # e.g. unencodable input (lone surrogates): the heuristic keeps
            # the counter total — never raising is the contract.
            return heuristic_token_count(text)

    return count


def _load_encoder(model_name: str) -> tiktoken.Encoding | None:
    """The model's encoding, or None when none can be built (never raises).

    `encoding_for_model` raises KeyError on unknown / non-OpenAI names; the
    default encoding covers those. Any other failure — the first-use BPE
    download cannot complete (offline), the data is corrupt — returns None so
    the caller degrades to the heuristic counter."""
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        pass  # unmapped model name: try the default encoding below
    except Exception:
        return None
    try:
        return tiktoken.get_encoding(_FALLBACK_ENCODING)
    except Exception:
        return None


def build_token_counter(model_name: str) -> Callable[[str], int]:
    """`text -> token count` for `model_name`; never raises, offline-safe.

    Uses the model's tiktoken encoding when the name maps to one, the default
    encoding when it does not, and the deterministic char/CJK heuristic when
    no encoding can be loaded at all (e.g. offline first use).
    """
    encoder = _load_encoder(model_name)
    if encoder is None:
        return heuristic_token_count
    return _counter_from_encoder(encoder)
