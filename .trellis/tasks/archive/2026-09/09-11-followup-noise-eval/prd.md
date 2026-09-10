# Re-evaluate residual retrieval noise on follow-up queries

## Goal

Now that the retrieval noise gates (archived `09-10-irrelevant-query-noise-gates`
and `09-10-vector-rescue-bm25-backstop`) and history-aware query rewriting are
all shipped, **measure** how much retrieval noise remains specifically on
multi-turn **follow-up** queries, and decide — with evidence — whether any
further gating/tuning is warranted. This is direction #4 of the multi-turn
hallucination-risks issue ("After issue #2 is fixed, re-evaluate residual noise
pressure on follow-up queries").

This is an **evaluation-first** task: its primary deliverable is a recorded,
reproducible measurement and a go/no-go decision. Any production code change is
**conditional** on the findings and, if non-trivial, spun out as its own task.

## Background — what is already in place (source-verified)

- **Noise gates (shipped).** Vector rescue only fires on-domain
  (`SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE=0.62`) and only when the BM25 leg
  is empty; the BM25 identity group respects term coverage. The five archived
  out-of-domain queries return 0 items (`rag/retriever.py`, `search/queries.py`).
- **Query rewriting (shipped).** `ChatService._rewrite_query` resolves
  anaphora in follow-ups into a self-contained retrieval query before the run
  (`services/chat.py`), reducing the referent-less "weak query" pressure the
  gates otherwise absorb.
- **Measurement precedent.** The 09-10 tasks calibrated against the **real
  15-doc corpus** via `GET /api/v1/search?q=...` live probes and recorded the
  evidence in their `design.md`. `tests/corpus.py` is a 2-doc toy for unit
  tests, not the evaluation corpus.

The open loop: rewriting changes what actually reaches the retriever on
follow-ups, so the residual noise on the *rewritten* queries has not been
measured. Direction #4 asks exactly this.

## Scope

In scope: define a reproducible follow-up evaluation set; measure retrieval
noise on follow-ups **with** rewriting vs a raw-anaphoric baseline vs the
resolved-by-hand ideal; record findings and a decision. Out of scope: building
a permanent automated eval framework, and any speculative gate change made
before the measurement justifies it.

## Requirements

### R1 — A reproducible follow-up evaluation set

- A small, documented set of multi-turn scenarios over the real corpus: each
  scenario is a topic-establishing turn plus one or more anaphoric follow-ups
  (e.g. `Redis 分布式锁` → `那它的缺点呢?` / `和另一种方式比呢?`), including at
  least one topic-shift and one genuinely out-of-domain follow-up.
- Each follow-up is labeled with its expected on-topic document(s) so noise
  (off-topic admitted chunks) can be counted.

### R2 — Three-way measurement per follow-up

- For each follow-up, capture the retrieved result set (count + per-item
  relevance) under three conditions:
  1. **raw anaphoric** (rewriting disabled) — the pre-rewrite baseline;
  2. **rewritten** (rewriting enabled, as shipped) — the current behavior;
  3. **hand-resolved ideal** — a manually written standalone query, the
     achievable ceiling.
- Metrics: noise count (off-topic chunks admitted), whether the on-topic
  document(s) were retrieved (recall), and result-set size — reported per
  follow-up and aggregated.

### R3 — Recorded findings + decision (the deliverable)

- Findings are written into this task (a `findings.md` or the task journal) and
  the evidence table recorded, mirroring the 09-10 evidence style.
- A clear decision: (a) residual noise is acceptable → close with no code
  change; or (b) a specific residual pattern warrants a targeted fix → open a
  new, scoped task describing it. The decision must cite the measured numbers.

### R4 — Reproducibility & hygiene

- The exact queries, corpus state, endpoint/flags, and how to toggle rewriting
  (`KB_CHAT_QUERY_REWRITE_ENABLED`) are documented so the run can be repeated.
- No query text or corpus content is committed to logs; the evaluation writes
  its own artifact intentionally (that is data, not logging).

## Non-goals

- Building a standing eval harness / CI gate (could be a later task if the ad
  hoc run proves valuable).
- Changing retrieval gates, rewriting, or history handling speculatively —
  changes only follow evidence, as a separate task.
- Re-litigating the 09-10 out-of-domain single-turn results (already covered by
  their tests).

## Acceptance Criteria

- [ ] AC1: A documented follow-up evaluation set (R1) exists with per-follow-up
      expected on-topic documents.
- [ ] AC2: The three-way measurement (R2) is executed against the real corpus
      and the per-follow-up + aggregate results are recorded in the task.
- [ ] AC3: The findings quantify (i) how much rewriting reduces follow-up noise
      vs the raw baseline and (ii) the residual gap vs the hand-resolved ideal.
- [ ] AC4: A go/no-go decision is recorded with cited numbers; if "go", a
      scoped follow-up task is created describing the specific fix.
- [ ] AC5: The run is reproducible from the recorded procedure (R4).

## Open questions

- Q1 (corpus): reuse the existing real 15-doc corpus used by 09-10, or refresh
  it? Recommendation: reuse for continuity with the 09-10 evidence.
- Q2 (measurement surface): measure at the retrieval boundary
  (`GET /api/v1/search` on the rewritten query) vs end-to-end via chat SSE
  `sources` events. Recommendation: primarily the search endpoint on the
  rewritten query (isolates retrieval noise), with a couple of end-to-end chat
  spot-checks for realism.
- Q3 (relevance labeling): manual per-item judgement (small N) vs a fixed
  expected-doc set per follow-up. Recommendation: expected-doc set for counting
  recall + manual off-topic tally for noise.
- Q4 (automation): keep the run as a documented manual procedure vs a small
  throwaway script. Recommendation: a documented procedure now; scripting only
  if a standing harness is later greenlit.
