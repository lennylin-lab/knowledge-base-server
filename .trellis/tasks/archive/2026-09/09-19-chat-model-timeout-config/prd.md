# Chat model timeout/retry configurable for long structured-output requests

## Goal

Make the chat model's request timeout and retry count Settings-configurable with defaults that can absorb long non-streaming structured-output calls, so draft (续写) no longer fails with `ModelAPIError: Request timed out` after ~181s.

## Background / Confirmed facts

- Production log (2026-09-18): draft run failed with `ModelAPIError: Request timed out`, `error_class=ModelAPIError`, ~181s after the model call started.
- Root cause: `src/app/llm/models.py` hardcodes `_REQUEST_TIMEOUT = 60.0` and `_MAX_RETRIES = 2` in the `AsyncOpenAI` client. Draft uses non-streaming structured output (full `DraftContent` in one completion); qwen took >60s per attempt, so first call + 2 SDK retries all timed out (3 × 60s ≈ 181s observed).
- Not a regression from the SSE change: the pre-SSE synchronous endpoint used the same model factory and would have hit the same wall; failure handling (operation durably `failed`, resumable) worked as designed.
- Chat `ask` streams, so it is largely insensitive to total-duration timeouts; structured-output endpoints (draft, and associations/summarize which share this model) are the exposed surface.
- Spec anchor: `.trellis/spec/backend/quality-guidelines.md` (~line 184) — SDK timeout/retry defaults live as provider-layer constants in `llm/`, one factory, one place.

## Requirements

- Add Settings knobs (e.g. `CHAT_REQUEST_TIMEOUT`, `CHAT_MAX_RETRIES`) wired into `AsyncOpenAI` in `llm/models.py`; no other construction site may appear.
- Defaults sized for long structured outputs: timeout ≥ 300s; retries low (0–1) — retrying a timed-out multi-minute generation mostly wastes budget.
- Do not change `llm/embeddings.py` behavior (its 60s + retries fit short embedding calls), but keep the "configured in exactly one place" contract intact.
- No changes to SSE event vocabulary, persistence timing, or error taxonomy.

## Acceptance Criteria

- [ ] `uv run mypy src` strict and `uv run ruff check .` pass.
- [ ] Focused test asserts the client is built from Settings values (timeout/retries), not hardcoded literals.
- [ ] Full test suite passes (the pre-existing env-only RBAC failure is out of scope).

## Out of scope

- Streaming-based structured output.
- Embeddings timeout changes.
- Frontend changes.

## Key decisions

- Config-over-code: values come from Settings with sane larger defaults; keep the single-construction-site rule.
