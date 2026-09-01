# Implement: Provider Config Isolation

Lightweight task — PRD-only. Mechanical rename + wire-up, single commit.

## Checklist

- [ ] `core/config.py`: replace `OPENAI_BASE_URL`/`OPENAI_API_KEY` with
      `EMBEDDING_BASE_URL`/`EMBEDDING_API_KEY` + `CHAT_BASE_URL`/`CHAT_API_KEY`
- [ ] `llm/embeddings.py` + `llm/models.py`: read their own variables
- [ ] `api/deps.py`: chat availability keyed on `CHAT_API_KEY`; embedding
      degradation keyed on `EMBEDDING_API_KEY` (message strings updated)
- [ ] `.env.example`: six new variables, none of the old two
- [ ] Tests: `test_embeddings.py`, `test_search_api.py`,
      `test_mcp_lifespan.py`, `test_qa_agent.py`, `test_chat_api.py` —
      rename constructor kwargs / Settings fields; keep fakes untouched
- [ ] Spec: `directory-structure.md` example snippet mentions the old
      names — update
- [ ] Validation: `grep -rn "OPENAI_BASE_URL\|OPENAI_API_KEY" src/ tests/` empty;
      full gates: ruff check / format --check / mypy src / pytest

## Rollback

Single revert; `.env` users must switch variable names (breaking change,
documented in commit message).
