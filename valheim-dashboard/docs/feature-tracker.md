# Dashboard feature tracker

This file is the repository-local implementation tracker. The session plan
remains the architecture reference; this tracker records work in this source
repository and is suitable for review in VS Code.

Feature records, delivery gates, and the initial feature queue are defined in
[the development harness](./development-harness.md).

## Workspace bootstrap

- [x] Independent Git repository initialized.
- [x] Python backend isolated under `backend/`.
- [x] Vite React frontend isolated under `frontend/`.
- [x] Localhost-only backend health harness and frontend health view added.
- [x] Backend/unit-test and frontend development tasks configured for VS Code.
- [x] Immutable Git-revision deployment tooling and localhost-only production
  service definition implemented and verified.
- [x] Root-owned release layout and restricted `valheim-ui` account activated
  from a clean committed revision (2026-09-13).

## Core modpack dependency

- [x] Hexium-only manifest and lock resolver complete.
- [x] Verified staging, atomic activation, and rollback complete.
- [x] Existing twelve-hour maintenance service orchestration complete.
- [x] Structured, bounded status snapshots exported and observed from the
  root-owned maintenance path.

## Dashboard features

- [x] Frontend-to-backend development proxy for `/api/*`.
- [x] Read-only server, release, maintenance, and bounded-log views.
- [x] Pending Hexium manifest editor and dependency preview.
- [x] Confirmed queue-for-next-maintenance action.
- [x] Scoped, confirmed immediate-maintenance action.
- [x] Scoped, confirmed rollback action.
- [x] CSRF, origin/host validation, audit logging, and strict request validation.

## Feature status

| ID | Scope | Status | Verification |
| --- | --- | --- | --- |
| `dashboard-health` | full-stack | verified | Backend unit tests, frontend lint/build, live Vite `/api/healthz` proxy request |
| `dashboard-deployment` | full-stack | verified | Production static/API backend tests, frontend lint/build, shell/unit validation, immutable release deployment, `valheim-ui` access checks, and loopback HTTP verification |
| `server-status` | full-stack | verified | Dashboard's 14 backend tests, modpack's 17 tests, and frontend lint/production build pass; clean-commit production deployment and live `/api/status` plus `/api/server-status` verification complete. |
| `maintenance-status` | full-stack | verified | Dashboard's 14 backend tests, modpack's 17 tests, and frontend lint/production build pass; clean-commit production deployment and live `/api/status` plus `/api/maintenance-status` verification complete without changing the timer cadence. |
| `modpack-status` | full-stack | verified | Dashboard's 14 backend tests, modpack's 17 tests, and frontend lint/production build pass; clean-commit production deployment and live `/api/status` plus `/api/modpack-status` verification complete. |
| `status-events` | full-stack | verified | Replaced the 60-second `/api/status` interval with a bounded same-origin SSE signal. The backend emits only fixed connection, snapshot-change, and heartbeat events after checking non-sensitive metadata of the approved status export; the UI retains the last successful view while a changed snapshot is loaded. Stream setup and replacement signaling are covered by backend tests (90/90); frontend lint/build pass. The deployed `:8080` SSE endpoint returned only the fixed connection event, and the forwarded browser retained the overview with zero completed API fetches during a 65-second observation past the previous polling interval. |
| `operations-overview` | full-stack | verified | Loading, unavailable, malformed-data-safe, empty, and ready source states implemented. Forwarded integrated-browser access to the deployed production `:8080` page confirms the corrected padded, responsive action layout and its visible controls (2026-09-13); `npm run lint` and `npm run build` pass. |
| `sessions-csrf-origin` | backend | in-progress | `SessionStore`/`require_safe_state_change` implemented: exact Host/Origin/Content-Type checks, CSRF token bound to session cookie via `secrets.compare_digest`. 48 new unit tests (`test_security.py`, `test_audit.py`, `test_pending.py`) plus an HTTP-level `PendingWorkflowTests` class pass (75/75 backend tests total); not yet deployed/verified live. |
| `bounded-audit-trail` | backend | in-progress | `audit.record_event`/`read_recent_events` bound the log to 200 events and 200 characters of detail with atomic rewrite; covered by unit and HTTP-level tests; not yet deployed/verified live. |
| `pending-manifest` | full-stack | in-progress | Fixed-schema validation, CSRF/origin/session enforcement, duplicate-request conflict handling, and audit recording covered by backend tests; frontend `ManifestEditor` implemented, lint/build clean. Modpack-side `apply_pending_manifest()` and root wrapper script implemented and unit/sandbox-tested but not installed. Not yet deployed or live-verified. |
| `update-request` | full-stack | in-progress | Confirmation-word gate, CSRF/origin/session enforcement, and audit recording covered by backend tests; frontend action submits an immediate maintenance run through `valheim-restart.service`, reusing the same resolver, activation, rollback, and readiness checks as the scheduled timer. |
| `rollback-request` | full-stack | in-progress | Confirmation-word gate, CSRF/origin/session enforcement, and audit recording covered by backend tests; frontend `RollbackAction` implemented. Root `valheim-rollback-request` helper implemented sharing the restart cooldown and health check, verified via sandboxed smoke tests; not installed/enabled — restart-capable helpers require explicit authorization before being triggered on the live host. |
| `world-backups` | partially verified | Bounded root-exported native-backup inventory and opaque-ID restore transport implemented. The deployed `:8080` dashboard displays four structurally-ready native backups without paths/names; selection plus exact `RESTORE` confirmation enables Restore, then clears safely without submission. The non-disruptive inventory publisher is installed/enabled. Root transactional restore/recovery source is scratch-tested but its units remain uninstalled/disabled pending explicit authorization because restore stops and starts Valheim. |
| `hexium-package-search` | verified | Fixed-host, redirect-rejecting, bounded catalogue search caches the Hexium v1 package list for 15 minutes and returns at most 10 allow-listed matches. The UI accepts a package name or an allow-listed Hexium `/mods/<namespace>/<package>` link, renders matches, and requires explicit `Add to manifest` selection before it builds a selection-only upcoming-manifest preview. Raw package/version/channel/role entry has been removed. The deployed forwarded `:8080` page verified the empty state, live matches, explicit add into the preview, removal, reload discard, and no pending manifest request. Backend tests (88/88), frontend lint/build pass. |

## Validation

- [x] Development harness responds only on localhost.
- [x] Dashboard unit has no public listener.
- [ ] SSH-tunnel access documented and tested.
- [x] Invalid requests and CSRF attempts are rejected (unit + HTTP-level
  tests: wrong Host, wrong Origin, wrong Content-Type, missing/mismatched
  CSRF token, and non-JSON bodies are all rejected before any pending
  request is written).
- [ ] Queued change deploys only during the established maintenance window.
