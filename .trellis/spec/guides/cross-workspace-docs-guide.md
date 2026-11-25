# Cross-Workspace Docs Guide

> **Purpose**: Keep cross-repo collaboration self-contained through `../knowledge-base-docs/` without reading sibling repositories' source code.

---

## Documentation Boundaries

| Scope | Location | Contents |
| --- | --- | --- |
| **Repo-local** | `docs/` (this repo) | Documentation applicable only to `knowledge-base-server` (internal design, backend runbooks, repo-specific specs) |
| **Cross-workspace** | `../knowledge-base-docs/` | System architecture (`architecture.md`) and cross-subsystem change handoffs (`subsystems/<target>/<YYYY-MM-DD>/<slug>.md`) |

Authoritative rules, `list.md` structure, and the change-note template live in `../knowledge-base-docs/README.md`.

---

## Before Planning / Implementing (Incoming Handoffs)

- [ ] When starting integration or adaptation work, check `../knowledge-base-docs/subsystems/server/list.md` for unchecked `- [ ]` items.
- [ ] Read the linked dated docs under `../knowledge-base-docs/subsystems/server/<YYYY-MM-DD>/` and consult `../knowledge-base-docs/architecture.md` if system boundaries are involved.
- [ ] Design and implement the adaptation from those self-contained handoff docs without reading sibling repositories' source code.

---

## Storage Dependency Policy (No Local Storage Orchestration)

- `knowledge-base-server` deployment orchestration (`docker-compose.yml`, `docker-compose.prod.yml`, `scripts/deploy.sh`) MUST NOT include storage services (`postgres`, `elasticsearch`, `redis`). External storage is owned by `knowledge-base-database` and `knowledge-base-deploy` (while `docker/elasticsearch/Dockerfile` and `.github/workflows/release.yml` continue publishing the ES+IK image to GHCR).
- Whenever a change in this repo adds or modifies storage dependencies — PostgreSQL version/extensions/roles/init requirements, Redis version/auth/persistence/key-prefix requirements, or Elasticsearch version/IK plugin image/mapping/reindex requirements — **both** `database` and `deploy` are mandatory handoff targets:
  - Write `../knowledge-base-docs/subsystems/database/<YYYY-MM-DD>/<slug>.md` and register `- [ ]` in `../knowledge-base-docs/subsystems/database/list.md`.
  - Write `../knowledge-base-docs/subsystems/deploy/<YYYY-MM-DD>/<slug>.md` and register `- [ ]` in `../knowledge-base-docs/subsystems/deploy/list.md`.

---

## Before Check / Finish (Outgoing Changes & Completion)

- [ ] If this task adapted an incoming item from `../knowledge-base-docs/subsystems/server/list.md`, mark that checklist entry `- [x]` after tests pass.
- [ ] If this task changes an external contract affecting another subsystem (`flutter`, `deploy`, `database`, `keycloak`, `gateway`) — such as `/api/v1/*` REST/SSE routes, DTOs, error envelopes, OIDC validation/claim requirements (`KB_OIDC_*`), `KB_*` environment variables, PostgreSQL/Redis/Elasticsearch requirements (including ES image/IK plugin or mapping/reindex changes), or container image/entrypoint/worker contracts:
  - Write a self-contained change note at `../knowledge-base-docs/subsystems/<target>/<YYYY-MM-DD>/<slug>.md` per `../knowledge-base-docs/README.md`.
  - Add an unchecked `- [ ]` entry under `## <YYYY-MM-DD>` (newest date first) in `../knowledge-base-docs/subsystems/<target>/list.md` for every affected target.
- [ ] **Strict boundary**: Describe only this repository's own changes, external contracts, compatibility, and verification steps in enough detail for consumers to adapt without reading this repo's source. Keep sibling repositories' source unread and leave their internal implementation changes to them.
