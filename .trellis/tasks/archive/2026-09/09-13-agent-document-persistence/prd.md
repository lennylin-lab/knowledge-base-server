# Agent document drafting and persistence

## Goal

Plan controlled agent-generated document drafts, revisions, and persistence with resumable interrupted runs.

## Confirmed facts
- Chat persists user before run and assistant only after success; failed user-only rows are excluded from complete-turn history (`src/app/services/chat.py`).
- WritingService streams suggestions but does not persist document changes (`src/app/services/agents.py`).
- Document writes have a service/repository boundary and async reindex lifecycle (`src/app/services/document.py`).

## Requirements
- Add independent agent-operation records with running/completed/failed/interrupted/applied states.
- Persist structured draft/patch content and target document base version for explicit inspection and resume.
- Keep drafts/interrupted runs out of ordinary chat history unless explicitly requested.
- Apply atomically with optimistic version checking and idempotency; reuse the indexing lifecycle.
- Provide draft creation, inspection, explicit apply, and resume; no silent auto-publish.

## Acceptance Criteria

- [ ] Data model distinguishes chat messages, operation runs, drafts, and published revisions.
- [ ] Interrupted operations are inspectable/resumable but excluded from normal history.
- [ ] Stale apply is rejected without mutation; duplicate apply is idempotent.
- [ ] Successful apply creates one revision and triggers indexing.
- [ ] Plan covers migration, service/API/tool/tests, observability, and rollback with no unresolved decisions.

## Out of scope
- Automatic publishing from partial model text, frontend, multi-user auth, or collaborative editing.
- Replacing chat persistence or recovering an in-flight provider request after process loss.

## Key decisions
- Explicit confirmation/apply is required; partial text is never a normal assistant history message.
- Version-based optimistic concurrency is mandatory.
