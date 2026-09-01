# Isolate Embedding vs Chat Model Provider Config

## Goal

`EMBEDDING_MODEL` and `CHAT_MODEL` currently share one
`OPENAI_BASE_URL` / `OPENAI_API_KEY` pair. Real deployments use different
providers for embeddings and chat (e.g. one vendor's embedding API + a
different chat endpoint), so each model gets its own base_url + api key.
The shared `OPENAI_*` variables are removed entirely (user decision:
clean replacement, no fallback).

## Requirements

1. **Settings** (`core/config.py`):
   - Embedding: `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` (SecretStr),
     `EMBEDDING_MODEL` (kept), `EMBEDDING_DIM` (kept)
   - Chat: `CHAT_BASE_URL`, `CHAT_API_KEY` (SecretStr), `CHAT_MODEL`
     (kept)
   - Remove: `OPENAI_BASE_URL`, `OPENAI_API_KEY`
2. **`llm/embeddings.py`**: provider reads
   `settings.EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY`.
3. **`llm/models.py`**: chat model reads `settings.CHAT_BASE_URL` /
   `CHAT_API_KEY`.
4. **Key-presence checks** (`api/deps.py` and anywhere else that tests
   for a configured provider): chat availability now checks
   `CHAT_API_KEY`; retriever degradation checks `EMBEDDING_API_KEY`.
   The two modes become independent — chat can be configured while
   embedding is not (bm25-only retrieval) and vice versa.
5. **`.env.example`**: update the LLM provider block to the new six
   variables (quality spec: every Settings group enumerated).
6. **Tests**: no test asserts the old variable names; settings tests
   (if any) updated; the existing fake-provider / stubbed tests should
   pass unchanged. Add a small unit pinning that each layer reads its
   own variables (e.g. changing only `CHAT_BASE_URL` affects the chat
   client only) if cheap; otherwise rely on construction-path coverage.

## Out of Scope

- Per-model retry/timeout overrides, provider-alias field
- Any behavior change beyond config plumbing (retrieval degradation
  semantics unchanged, just keyed off the new variable)
- README/docs rewrite (`.env.example` is the config surface this task)

## Acceptance Criteria

- [ ] `grep -r "OPENAI_BASE_URL\|OPENAI_API_KEY" src/ tests/` → no hits
- [ ] `uv run python -c` smoke: Settings() loads with only the new
      variables set; `OpenAIEmbeddingProvider` and the chat model
      construct against distinct base_urls when configured so
- [ ] Chat no-key ⇒ 503 `chat_unavailable` still works (keyed on
      `CHAT_API_KEY`); embedding no-key ⇒ search degrades to bm25 with
      `vector_search_disabled` (keyed on `EMBEDDING_API_KEY`)
- [ ] `.env.example` lists all six new variables, none of the old two
- [ ] Full gates green: ruff check / format --check / mypy src / pytest
