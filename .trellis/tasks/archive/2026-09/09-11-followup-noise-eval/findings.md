# Findings: residual retrieval noise on follow-up queries (measured 2026-09-11)

Evaluation deliverable for this task. Methodology per this task's `design.md`
(three-way measurement over the real corpus, mirroring the 09-10 live-probe
evidence style). No production code was changed; the throwaway driver/scratch
scripts lived under `/tmp` and were deleted after the run (the procedure below
is self-contained). One leftover of an aborted first attempt
(`/tmp/kb_followup_eval/` — a 7-scenario draft whose run ended entirely in
`llm_provider_error`, superseded by the final set below) was missed by the
implementer and removed during the check pass; `/tmp` is now clean of eval
scratch.

## Headline numbers (the decision hinges on these)

| Metric | Value |
|---|---|
| Mean noise per follow-up — **raw** anaphoric | **5.50** chunks |
| Mean noise per follow-up — **rewritten** (as shipped) | **3.17** chunks |
| Mean noise per follow-up — **hand-resolved ideal** | **2.67** chunks |
| **Rewrite lift** (raw − rewritten) | **2.33** chunks/follow-up (42% of raw noise gone) |
| **Residual gap** (rewritten − ideal) | **0.50** chunks/follow-up |
| Recall (expected doc retrieved), raw / rewritten / ideal | 3/6, **6/6**, 6/6 |
| Out-of-domain follow-up (`红烧肉怎么做才好吃?`) | **0 items in all three conditions** |
| Rank-1 hit on-topic (in-domain follow-ups), raw vs rewritten | 2/5 vs **5/5** |

Rewriting captures 2.33 of the 2.83 achievable noise reduction (**82%**) and
fixes all three recall failures of the raw baseline. **Decision: no-go** (see
Decision section).

## 1. Corpus identity and environment (reproducibility)

- 15 live documents in PostgreSQL, all `index_status=done` (reuses the 09-10
  corpus; Q1 decision: no refresh). Elasticsearch index `kb_documents` holds
  **39 chunk docs across 16 document_ids**: 37 chunks for the 15 live docs
  (matches the 09-10 record of "15 docs / 37 chunks") **plus 2 orphan chunks**
  of a deleted predecessor of the Kafka document (`01a088bd-a6c8-…`, same
  title as the live `01a08d4d-4384-…`). Orphan residue never surfaced in any
  probe below (and never in the live chat `sources`); recorded as a corpus
  hygiene note, not counted as noise.
- Server: `uv run uvicorn app.main:app` on `http://127.0.0.1:8000`, running
  throughout with default feature wiring:
  `KB_CHAT_QUERY_REWRITE_ENABLED` unset → **rewriting ON**,
  `KB_CHAT_REWRITE_HISTORY_TURNS` unset → 3 turns (6 messages).
- Gates (as shipped, from `.env`/defaults): `SEARCH_BM25_MIN_COVERAGE=70%`,
  `SEARCH_VECTOR_MAX_DISTANCE=0.45`, `SEARCH_VECTOR_RESCUE_MARGIN=0.15`,
  `SEARCH_VECTOR_RESCUE_MAX_DISTANCE=0.85`,
  `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE=0.62`,
  `SEARCH_RRF_MIN_RELATIVE=0.35`.
- Models: chat `gpt-5.5`, embeddings `Qwen/Qwen3-Embedding-4B`, via the
  configured OpenAI-compatible relay (URL/keys not recorded here).
- Measurement surface: `GET /api/v1/search?q=<query>&limit=8` — `limit=8`
  matches the chat pipeline's per-retrieval budget (`ChatRequest.limit`
  default). Live chat: `POST /api/v1/chat` (SSE: `run_started` / `sources` /
  `answer_delta` / `done` / `error`).
- The 15 live docs (short ids used in tables below):
  `01a08d4d` Kafka 架构原理与顺序、可靠性保障 · `01a088bd-aa05` Vue 3 核心机制 ·
  `-a991` Spring Boot 核心机制 · `-a91a` Redis 数据结构、缓存模式与高可用 ·
  `-a8a2` React 核心概念 · `-a82c` Python 语言特性与工程实践 ·
  `-a7b7` PostgreSQL 特性精要与 MySQL 的差异对比 · `-a740` MySQL 索引、事务与性能优化实践 ·
  `-a650` Java 核心知识点梳理 · `-a5dc` Golang 并发编程与工程实践 ·
  `-a564` Go Web 框架选型 · `-a4ed` 前端工程化 · `-a476` Flutter 跨平台开发 ·
  `-a3fc` FastAPI 异步框架 · `-a37c` Elasticsearch 原理、映射设计与查询调优.

## 2. Evaluation set (R1/AC1)

Six follow-ups over five axes. Each scenario = establishing chat turn(s) run
through the live chat endpoint (real LLM answers back the rewrite history),
then the follow-up. Expected on-topic sets are resolved against actual corpus
content (verified via ES `heading_path`/`chunk_text` before the run).

| # | Axis | Establishing turn(s) | Follow-up (raw) | Expected on-topic docs |
|---|------|----------------------|-----------------|------------------------|
| F1 | simple anaphora | `Redis 的分布式锁怎么实现?` | `那它的缺点呢?` | Redis `-a91a` |
| F2 | comparative anaphora | `MySQL 的事务隔离级别有哪些?` | `那 PostgreSQL 呢?` | PG-vs-MySQL `-a7b7` |
| F3 | topic shift mid-session | T1 `Redis 的缓存穿透怎么解决?`; T2 `Python 的装饰器是什么?` | `具体怎么写一个呢?` | Python `-a82c` |
| F4 | ellipsis | `Vue 3 的响应式系统是怎么工作的?` | `原理呢?` | Vue 3 `-aa05` |
| F5 | out-of-domain follow-up | `Kafka 怎么保证消息不丢?` | `红烧肉怎么做才好吃?` | ∅ (any hit is noise) |
| F6 | simple anaphora (2nd family) | `Golang 的 goroutine 和 channel 怎么用?` | `它的调度模型是怎样的?` | Golang `-a5dc` |

## 3. Condition queries (R4)

- **raw**: the verbatim follow-up (table above).
- **rewritten**: the ACTUAL string the shipped rewriter produced, captured
  offline via a scratch script that reproduced the shipped construction
  exactly: `build_rewrite_agent(get_chat_model(Settings()))` (from
  `src/app/agents/rewrite.py` + `src/app/llm/models.py`), called as
  `agent.run(followup, message_history=history[-6:])` where `history` is
  `to_message_history()` over the session's real persisted user+assistant
  rows (same slice rule as `ChatService._rewrite_query`, turns=3). Captured
  strings (LLM output, recorded verbatim — non-determinism caveat below):

  | # | Rewritten query | Rewrite verdict |
  |---|---|---|
  | F1 | `Redis 分布式锁的缺点是什么?` | correct resolution |
  | F2 | `PostgreSQL 的事务隔离级别有哪些？` | correct resolution |
  | F3 | `Python 装饰器具体怎么编写和使用？` | correct (survived topic shift) |
  | F4 | `Vue 3 响应式系统的底层原理是什么？` | correct resolution |
  | F5 | `红烧肉怎么做才好吃?` | no-op pass-through (prompt rule 2) |
  | F6 | `Golang 的 goroutine 的调度模型是怎样的?` | correct resolution |

- **ideal**: hand-written standalone queries, written with corpus knowledge
  (that is what makes them the ceiling): F1 `Redis 分布式锁有哪些缺点?` ·
  F2 `PostgreSQL 与 MySQL 事务隔离级别的差异` · F3 `Python 装饰器怎么编写?` ·
  F4 `Vue 3 响应式系统的实现原理` · F5 `红烧肉怎么做才好吃?` ·
  F6 `Golang goroutine 调度模型 GMP`.

  Note on F6: the ideal contains `GMP`, a term that appears neither in the
  establishing question nor in the establishing answer — the rewrite prompt
  forbids exactly that injection (rule 3: resolve from conversation only).
  The F6 ideal is therefore deliberately out of the rewriter's reach; see
  Decision.

## 4. Per-follow-up results (R2/AC2, AC3)

Noise = hits whose `document_id` is outside the expected set (document-set
rule; borderline items noted, rule decides). Recall = every expected doc has
≥1 hit. F5 recall is "n/a (0 expected; 0 observed = pass)".

| # | noise raw | noise rewritten | noise ideal | recall raw | recall rew | recall ideal | size raw/rew/ideal |
|---|---|---|---|---|---|---|---|
| F1 | 8 | 2 | 2 | no | yes | yes | 8 / 4 / 4 |
| F2 | 4 | 4 | 6 | yes | yes | yes | 6 / 6 / 8 |
| F3 | 8 | 3 | 3 | no | yes | yes | 8 / 6 / 6 |
| F4 | 6 | 5 | 5 | no | yes | yes | 6 / 7 / 7 |
| F5 | 0 | 0 | 0 | pass | pass | pass | 0 / 0 / 0 |
| F6 | 7 | 5 | 0 | yes | yes | yes | 8 / 8 / 3 |
| **mean** | **5.50** | **3.17** | **2.67** | 3/6 | 6/6 | 6/6 | 6.00 / 5.17 / 4.67 |

In-domain-only means (excluding F5, which contributes 0 everywhere):
raw 6.60, rewritten 3.80, ideal 3.20 — lift 2.80, residual gap 0.60.

Result-set detail (condensed; `doc` = short id + chunk):

- **F1 raw**: 8/8 noise, pure vector-leg admission of the programming cluster
  (Java ch2 r1, React ch2/ch1/ch0, Flutter ch2, Vue ch1/ch0, FastAPI ch1; all
  `vector_rank` only, `es_rank=None`) — the referent-less weak-query pressure
  in its pure form. **rewritten/ideal**: Redis ch1+ch0 at ranks 1–2 (two-leg),
  plus ES ch1 + Spring ch1 via the BM25 leg.
- **F2**: raw and rewritten admit the same noise trio (ES ch0, MySQL ch0+ch1,
  GoWeb ch1); PG doc rank 1 in both. MySQL-doc hits are borderline (the
  comparison counterpart) but count as noise by the doc-set rule. The ideal is
  the *noisiest* condition here (8 hits, adds Spring ch1 + React ch2).
- **F3 raw**: 8/8 noise from the vector leg (Flutter ch2 r1, Java, Vue ×2,
  React, Golang, GoWeb, ES) — zero lexical signal for `具体怎么写一个呢?`.
  **rewritten/ideal**: Python ch0/ch1/ch2 ranks 1/2/4 (BM25-only leg), noise =
  3 × FastAPI (ch0/ch2/ch1) — FastAPI's content mentions decorators, a
  genuinely close topical neighbor.
- **F4 raw**: `原理呢?` is lexically captured by documents with 原理 in the
  title — 6/6 noise, BM25-only (ES ch0/ch1/ch2, Kafka ch0/ch1, Spring ch0).
  **rewritten/ideal**: Vue ch0+ch1 ranks 1–2 (two-leg); noise = React ch2
  (the "与 Vue 的差异速记" chunk — borderline, genuinely Vue-adjacent),
  frontend ch0, Kafka ch0, Python ch2, Java ch2.
- **F5**: 0 items in all three conditions — the 09-10 gates hold unchanged on
  an out-of-domain *follow-up* (the query is self-contained, so this equals
  the already-tested single-turn case).
- **F6**: raw keeps Golang ch0 at rank 1 via the BM25 leg but adds 7 noise;
  rewritten keeps Golang ch0+ch1 at ranks 1–2 and adds a third on-topic Golang
  chunk (ch2) at rank 4 — 3 on-topic + 5 noise (Python ×2, Java, FastAPI,
  Flutter via BM25) = the recorded size 8 (re-verified live during the check
  pass); the keyword-dense ideal retrieves 3/3 Golang chunks, 0 noise.

Pattern worth noting: **rewritten == ideal noise sets in F1, F3, F4** (same
counts; F1 same two noise docs, F3 same three FastAPI chunks, F4 same five
noise docs) — on those the shipped rewriter's output is retrieval-equivalent
to the hand-written ceiling. F4's five noise chunks are identical under
rewritten AND ideal: that residual is the corpus's topical-cluster admission
property already measured and bounded in 09-10 (single-turn), not a
follow-up-specific or rewriting-specific effect.

## 5. End-to-end realism spot-checks (C4)

Three sessions continued live through `POST /api/v1/chat` (rewriting ON, as
shipped), judging the `sources` SSE events:

- **F1** (`那它的缺点呢?` continued): 3 retrieval batches; every batch led by
  Redis ch0/ch1, noise = ES ch1 + Spring ch1 (+ MySQL ch1 in one batch);
  `query_rewrite` log event shows original_length 7 → rewritten_length 15,
  i.e. the same resolution as the captured string. Matches the endpoint probe
  (Redis top-2, ES+Spring noise) exactly.
- **F3** (`具体怎么写一个呢?` continued): 2 batches; Python ch1/ch0 rank 1–2,
  noise = frontend ch0, FastAPI ch0/ch2/ch1 — matches the probe. First
  attempt hit a transient provider `ReadTimeout` mid-answer
  (`agent_run_failed`, error_class=ReadTimeout, after sources had flushed);
  the re-run completed `outcome=success` with identical retrieval shape.
  Eval-infrastructure flake, not a retrieval/rewrite behavior.
- **F5** (`红烧肉怎么做才好吃?` continued): **zero `sources` events**, short
  refusal-style answer, `outcome=success` — the out-of-domain follow-up
  returns nothing end-to-end. Matches.

Endpoint-level findings hold through the full pipeline.

## 6. Decision (R3/AC4): **no-go**

Applied rule (design.md): *rewritten noise at or near ideal noise (small
residual gap) AND out-of-domain follow-ups return 0 → no-go.*

1. **Residual gap is small: 0.50 chunks/follow-up** (3.17 vs 2.67). Rewriting
   captures 82% of the achievable noise reduction. On 3 of 5 in-domain
   follow-ups (F1/F3/F4) the rewritten query is retrieval-equivalent to the
   hand ideal; on F2 the rewriter actually beats the hand ideal (4 vs 6).
2. **Recall is at ceiling with rewriting: 6/6** vs raw 3/6 — the recall
   argument for touching anything further is negative; rewriting is the
   component that fixed recall.
3. **Out-of-domain follow-up returns 0** in every condition, endpoint and
   end-to-end — the 09-10 gates hold on follow-ups.

The only concentrated residual is **F6 (rewritten 5 vs ideal 0)**. Inspected
closely, it is not evidence of a rewriter defect: the gap exists because the
ideal query contains `GMP`, a term present nowhere in the conversation
(neither the question nor the answer mentions scheduling at all), and the
rewrite prompt *forbids* injecting such terms (rule 3). The rewriter's
`Golang 的 goroutine 的调度模型是怎样的?` still ranks the on-topic chunk #1
(two-leg, score 0.0328) with recall intact; the admitted chunks are the same
topical-cluster class that the 09-10 tasks measured single-turn and already
bounded (RRF-floor tightening was explicitly rejected there as global and
blunt). Recorded here as a **watched pattern** ("natural-language rewritten
queries admit somewhat more cluster noise than keyword-dense ones; F6 delta =
5 chunks at limit=8, no recall cost") rather than a fix task: N=1, no recall
impact, identical to an accepted single-turn property, and the candidate fix
(prompt tuning toward keyword-dense output) trades against the rewrite
contract's fidelity rules with no evidence base beyond this single row. If
future multi-turn work accumulates more instances of this pattern, a scoped
rewrite-prompt-refinement task can revisit it.

No production change; this task closes issue direction #4.

## 7. Caveats

- **Small N, directional evidence.** 15 docs, 6 follow-ups, one run — the
  same calibration posture as 09-10. Good enough for go/no-go, not for
  statistical claims.
- **LLM non-determinism.** Rewritten strings are one sample per follow-up;
  they are recorded verbatim above so every number is attributable to a
  concrete query. A different sample could shift per-follow-up noise by a
  chunk or two; the aggregate direction (large lift, small gap, recall fixed)
  is robust across the 5 correct resolutions observed.
- **Ideal queries are corpus-aware by construction** (written with knowledge
  of titles/headings); that is what "achievable ceiling" means, but it makes
  the ceiling partially unreachable by a history-bounded rewriter (F6 is the
  visible case; F2 shows the ceiling can also be worse than the rewriter).
- **Live providers required**; this evaluation is not part of the offline
  pytest suite and was not added as a CI gate (per task non-goals). The F3
  transient provider ReadTimeout illustrates why.
- **Validation gate note**: no pytest/ruff/mypy gate applies to this task —
  no production code was changed (repo footprint is this task's artifacts
  only).
- **Hygiene**: query/corpus text above is the evaluation's own artifact (data,
  not logging); nothing was written into app logs or git. (The app's own
  `session_created` event logs the session title — permitted by
  logging-guidelines, "title is loggable" — not an eval leak.) The user's WIP
  `tests/seed_data.py` / `tests/seeds/` was left untouched (committed
  independently by the user as `345108e` after this run).

## 8. Reproduction procedure (R4/AC5)

1. Environment: `docker compose up -d` (postgres/elasticsearch/redis); corpus
   of the 15 docs listed in §1 indexed (`index_status=done`); server on
   `:8000` with the settings of §1 (rewriting ON by default).
2. Record corpus identity: `GET /api/v1/documents?limit=100` (15 items);
   `curl localhost:9200/kb_documents/_count` (39, incl. the 2 orphans).
3. For each scenario: `POST /api/v1/chat` `{"question": <establishing>, "limit": 8}`
   (F3 sends T1 then T2 with `session_id` from T1's `run_started`); keep the
   assistant answer text from `answer_delta` events.
4. Rewritten queries: in a scratch script (repo-root venv), build
   `build_rewrite_agent(get_chat_model(Settings()))`, assemble
   `to_message_history([user, assistant, …])` from the real session rows,
   call `agent.run(followup, message_history=history[-6:])`, record
   `result.output.strip()`.
5. For every (follow-up × {raw, rewritten, ideal}): `GET
   /api/v1/search?q=<urlencoded>&limit=8`; classify each hit
   on/off-topic by `document_id` against §2's expected sets; compute noise,
   recall, size as in §4.
6. Spot-checks: continue the F1/F3/F5 sessions with the follow-up via
   `POST /api/v1/chat` and compare `sources` events with step 5's probes.

Expected outcome when reproduced: the tables of §4 (exact per-chunk noise sets
may vary with LLM samples per the caveat above; the aggregate pattern — raw
recall failures fixed by rewriting, mean noise 5.5 → ~3 → ~2.7, out-of-domain
0 — should hold).
