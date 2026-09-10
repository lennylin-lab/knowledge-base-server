# Design: Follow-up residual-noise evaluation (methodology)

This is an evaluation task, so the "design" is the **measurement methodology**:
what to measure, how, against what, and how a decision is derived. It reuses
the 09-10 real-corpus live-probe precedent and the shipped rewrite toggle.

## Measurement surface (Q2)

Primary: `GET /api/v1/search?q=<query>&limit=<N>` — measures retrieval noise
directly on a given query string, isolating the retriever from LLM answer
behavior. Follow-ups are evaluated by feeding the search endpoint the query
each condition produces:

- **raw anaphoric**: the verbatim follow-up (e.g. `那它的缺点呢?`).
- **rewritten**: the standalone query the shipped rewriter would produce. Two
  ways to obtain it, in preference order:
  1. capture it from a real chat turn — run the follow-up through
     `POST /api/v1/chat` (or the SSE endpoint) with
     `KB_CHAT_QUERY_REWRITE_ENABLED=true` and read the query the retriever saw
     (temporary debug log of the rewritten query length + value in a dev-only
     run, or the `sources` it returns), or
  2. reproduce it offline by calling the rewrite agent directly in a scratch
     script with the same recent-history.
- **hand-resolved ideal**: a human-written standalone query capturing the true
  intent — the achievable ceiling.

Secondary (realism spot-checks): a few end-to-end chat runs, inspecting the
`sources` SSE events to confirm the endpoint-level findings hold in the full
pipeline (rewrite → retrieve → gate → answer).

## Evaluation set (R1)

A documented table of scenarios over the real 15-doc corpus (Q1: reuse it).
Each row: establishing turn, follow-up, condition-specific query, and the
expected on-topic document(s). Cover the axes that stress rewriting + gates:

| Axis | Example scenario |
|------|------------------|
| Simple anaphora | `Redis 分布式锁` → `那它的缺点呢?` (expect: Redis doc) |
| Comparative anaphora | `MySQL 事务隔离` → `和另一种方式比呢?` |
| Topic shift mid-session | `Redis…` → (new) `Python 的装饰器` |
| Ellipsis | `Vue 3 响应式` → `原理呢?` |
| Out-of-domain follow-up | `Redis…` → `红烧肉怎么做?` (expect: 0 items) |

The set is small (≈5–8 follow-ups) and fully listed in the task findings so it
is reproducible and reviewable.

## Metrics (R2/R3)

Per follow-up, per condition (raw / rewritten / ideal):

- **noise**: count of retrieved chunks whose document is NOT in the expected
  on-topic set (for out-of-domain follow-ups, any hit is noise).
- **recall**: did the retrieval include ≥1 chunk from each expected on-topic
  document (yes/no).
- **size**: total retrieved chunks.

Aggregate: mean noise per follow-up for each condition, and the two headline
numbers the decision hinges on:

- **rewrite lift** = raw.noise − rewritten.noise (how much rewriting helps).
- **residual gap** = rewritten.noise − ideal.noise (what rewriting still leaves
  on the table).

## Relevance labeling (Q3)

Expected-on-topic **document set** per follow-up (not per chunk) drives recall
and the noise tally: a chunk counts as noise iff its `document_id` is outside
the expected set. This is objective and reproducible; borderline items are
noted but the document-set rule decides the count.

## Decision rule (R3/AC4)

- If **rewritten.noise** is at or near **ideal.noise** across the set (small
  residual gap) and out-of-domain follow-ups return 0 → **no-go**: residual
  noise is acceptable; close the task with the evidence, no code change.
- If a **specific, repeatable** residual pattern remains (e.g. comparative
  anaphora under-resolves and admits a consistent off-topic cluster) → **go**:
  open a new scoped task describing that pattern, its measured cost, and a
  candidate fix (gate tweak, rewrite-prompt refinement, or history-extent
  change). The decision cites the numbers.

## Reproducibility (R4)

The findings record: corpus identity/state, the exact query strings for all
three conditions, `limit`, the endpoint(s), and the rewrite toggle used
(`KB_CHAT_QUERY_REWRITE_ENABLED`). Anyone can re-run the same probes and get the
same table. If a throwaway script is used (Q4), it lives under a scratch path
and is not part of `src/` (no standing harness in this task).

## What this task deliberately does NOT change

- No edits to `rag/retriever.py`, `search/queries.py`, `services/chat.py`, or
  config — unless the decision is "go" and even then via a separate task. This
  task's repo footprint is the task artifacts (+ optional throwaway script),
  not production code.

## Risks / caveats

- **Small corpus / small N.** 15 docs and a handful of follow-ups give
  directional, not statistical, evidence — enough for a go/no-go, consistent
  with how 09-10 was calibrated. Stated explicitly in the findings.
- **Rewrite non-determinism.** The rewrite is an LLM call; capture the actual
  rewritten string used so the measurement is attributable to a concrete query,
  not a re-sampled one.
- **Live LLM/embeddings required.** Unlike the shipped tasks' offline unit
  tests, this evaluation needs the real providers + corpus; it is a manual/live
  procedure, not part of the offline suite.
