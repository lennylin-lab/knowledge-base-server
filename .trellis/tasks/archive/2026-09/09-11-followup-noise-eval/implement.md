# Execution plan (evaluation runbook)

This task executes a measurement, not a code change. "Implementation" is the
evaluation procedure; the output is recorded findings + a decision. Any code
change is a separate, evidence-driven follow-up task.

## Stage A — Prepare the evaluation set

- [ ] A1. Confirm/seed the real 15-doc corpus (reuse the 09-10 corpus, Q1).
      Record its identity/state (doc count, chunk count).
- [ ] A2. Write the follow-up scenario table (≈5–8 follow-ups) covering simple
      anaphora, comparative anaphora, topic shift, ellipsis, and an
      out-of-domain follow-up. For each, record: establishing turn, follow-up
      text, and expected on-topic document set.

## Stage B — Capture the three condition queries per follow-up

- [ ] B1. **raw anaphoric**: the verbatim follow-up string.
- [ ] B2. **rewritten**: obtain the actual standalone query the shipped
      rewriter produces for each follow-up given the establishing turn — either
      by capturing it from a real chat run with
      `KB_CHAT_QUERY_REWRITE_ENABLED=true`, or by calling the rewrite agent in a
      scratch script with the same recent history. Record the exact string.
- [ ] B3. **hand-resolved ideal**: write the human standalone query per
      follow-up.

## Stage C — Measure

- [ ] C1. For every (follow-up × condition) query, call
      `GET /api/v1/search?q=<query>&limit=<N>` and record: total hits, and per
      hit its `document_id`/title (to classify on-topic vs noise).
- [ ] C2. Compute per-follow-up metrics: noise count (docs outside the expected
      set), recall (each expected doc hit?), size.
- [ ] C3. Aggregate: mean noise per condition, **rewrite lift**
      (raw−rewritten) and **residual gap** (rewritten−ideal).
- [ ] C4. Secondary realism check: run 2–3 follow-ups end-to-end via chat and
      confirm the `sources` events match the endpoint-level findings.

## Stage D — Record findings

- [ ] D1. Write `findings.md` in this task dir (or the developer journal): the
      scenario table, the per-follow-up + aggregate results, the two headline
      numbers, and all reproducibility details (corpus state, exact queries,
      `limit`, endpoints, rewrite toggle) — 09-10 evidence style.

## Stage E — Decide (the gate)

- [ ] E1. Apply the decision rule (design.md):
      - **no-go** (residual acceptable, out-of-domain follow-ups return 0):
        record the conclusion; this task closes with no production change.
      - **go** (a specific repeatable residual pattern with measured cost):
        create a new scoped task (`task.py create ...`) describing the pattern,
        its numbers, and a candidate fix (gate tweak / rewrite-prompt refine /
        history-extent change). Do NOT implement the fix in this task.
- [ ] E2. If any durable lesson emerges (e.g. a follow-up class the rewriter
      systematically under-resolves), capture it into `.trellis/spec/` via the
      spec-update flow so future retrieval/chat work inherits it.

## Validation

- [ ] V1. A second person (or a fresh run) can reproduce the table from the
      recorded procedure (AC5).

## Notes / constraints

- Live providers + real corpus required; this is NOT part of the offline
  `pytest` suite. Do not add it as a CI gate in this task.
- No speculative edits to `rag/`, `search/`, `services/chat.py`, or config in
  this task — changes only via the follow-up task if the decision is "go".
- Capture the ACTUAL rewritten strings used (LLM non-determinism) so numbers are
  attributable to concrete queries.

## Rollback

- Nothing to roll back: the task's repo footprint is artifacts (+ optional
  throwaway script outside `src/`). If a scratch script was added, delete it on
  completion.

## Dependency note

- Depends on shipped work: noise gates (09-10 ×2) and query rewriting. This
  task closes the issue's direction #4 loop and may spawn one targeted
  follow-up if evidence warrants; it is otherwise terminal.
