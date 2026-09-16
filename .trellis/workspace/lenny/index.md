# Workspace Index - lenny

> Journal tracking for AI development sessions.

---

## Current Status

<!-- @@@auto:current-status -->
- **Active File**: `journal-1.md`
- **Total Sessions**: 22
- **Last Active**: 2026-09-16
<!-- @@@/auto:current-status -->

---

## Active Documents

<!-- @@@auto:active-documents -->
| File | Lines | Status |
|------|-------|--------|
| `journal-1.md` | ~502 | Active |
<!-- @@@/auto:active-documents -->

---

## Session History

<!-- @@@auto:session-history -->
| # | Date | Title | Commits | Branch |
|---|------|-------|---------|--------|
| 22 | 2026-09-16 | Gateway issue #2 retests passed; real-model e2e acceptance done | `1f63d21` | `main` |
| 21 | 2026-09-16 | Gateway v1.2 integration and defect filing | `1f63d21` | `main` |
| 20 | 2026-09-15 | Document summary/associations SSE endpoints | `a16d933` | `main` |
| 19 | 2026-09-15 | Gateway identity, tenancy, and RBAC implemented | `5dcd7ce` | `main` |
| 18 | 2026-09-15 | Gateway integration verified end-to-end | `ea58301` | `main` |
| 17 | 2026-09-14 | Agent document drafting and persistence | `ddbc8b2`, `8e0e32b` | `feat/agent-document-persistence` |
| 16 | 2026-09-13 | Chat SSE progress events | `33ba8c9`, `657c8d0`, `4483760` | `main` |
| 15 | 2026-09-12 | Redis cache layer for embeddings, agents, and search | `1d3dbff` | `-` |
| 14 | 2026-09-12 | Carry prior-run sources into follow-up turns | `a197744`, `8497f67`, `19a7e05` | `feat/sources-carry-forward` |
| 13 | 2026-09-11 | Follow-up residual-noise evaluation (direction #4, no-go) | `998cf21` | `main` |
| 12 | 2026-09-11 | Rolling conversation summary beyond the history window | `19bcc6f`, `f410869`, `33c14c1`, `b000064` | `main` |
| 11 | 2026-09-11 | Token-based history budget + long-document guardrail | `c990389`, `eda8061`, `d2103b5`, `07cdc0b` | `main` |
| 10 | 2026-09-11 | History-aware query rewriting for chat follow-ups | `56d85d5`, `0f75035`, `f7d4648`, `9ba7898` | `main` |
| 9 | 2026-09-10 | Vector rescue scoped to lexical-failure backstop (BM25-empty gate) | `f66870a`, `5e128e7`, `066b3e6` | `main` |
| 8 | 2026-09-10 | Close irrelevant-query noise gates: BM25 identity coverage + vector rescue on-domain trigger (0.62) | `aef7bea`, `9db9635`, `964dcd0`, `8d84e1b`, `60bc581` | `main` |
| 7 | 2026-09-10 | Complete Trellis onboarding (00-join-lenny) | - | `main` |
| 6 | 2026-09-08 | ES BM25 scoring overhaul: cross-field evidence and coverage gate | `bf03a23`, `0d599ad` | `main` |
| 5 | 2026-09-07 | Code-aware markdown chunking + code-friendly ES index | `47be781`, `4e933c4`, `365f8f5` | `main` |
| 4 | 2026-09-06 | ES IK analyzer for Chinese corpus | `5a09cf4`, `efd3a70` | `main` |
| 3 | 2026-09-06 | Short-query vector gate calibration | `bba8dad`, `77c8693` | `main` |
| 2 | 2026-09-05 | Search relevance quality gates | `09e2c63`, `fb77ee4` | `main` |
| 1 | 2026-09-05 | Content hash guard skips duplicate reindexing | `c2b031c`, `b66aae1` | `main` |
<!-- @@@/auto:session-history -->

---

## Notes

- Sessions are appended to journal files
- New journal file created when current exceeds 2000 lines
- Use `add_session.py` to record sessions