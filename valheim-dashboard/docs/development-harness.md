# Development Harness

This harness turns the dashboard roadmap into independently reviewable
features. It is designed for use with the repository's VS Code workspace and
GitHub Copilot instructions.

## Feature record

Add one record to [`feature-tracker.md`](./feature-tracker.md) for every
deliverable. A feature advances only when all applicable gates are complete.

| Field | Required content |
| --- | --- |
| ID | Stable kebab-case identifier |
| Scope | `backend`, `frontend`, or `full-stack` |
| Intent | Operator-facing outcome, not an implementation detail |
| API contract | Route, payload shape, success response, error cases |
| Security boundary | Authorization, validation, confirmation, and audit rules |
| Verification | Exact test, lint, build, and manual checks |
| Status | `planned`, `in-progress`, `verified`, or `blocked` |

## Delivery gates

### Backend feature

1. Define the API and validation behavior.
2. Add unit tests covering success and rejected input.
3. Implement the route without expanding filesystem or systemd privileges.
4. Run the backend test task.

### Frontend feature

1. Confirm the required backend contract is verified.
2. Implement loading, empty, successful, and error states.
3. Use same-origin `/api/` calls only.
4. Run frontend lint and production build.

### State-changing feature

1. Define strict request validation and operator confirmation.
2. Require CSRF plus Origin/Host validation.
3. Record a bounded audit event with outcome.
4. Prove invalid, missing-confirmation, and CSRF-invalid requests fail.
5. Confirm the action cannot bypass the scheduled mod-maintenance lifecycle.

## Initial feature queue

| ID | Scope | Intent | Status |
| --- | --- | --- | --- |
| `server-status` | full-stack | Show bounded exported server/service state. | planned |
| `maintenance-status` | full-stack | Show next maintenance and last outcome. | planned |
| `modpack-status` | full-stack | Show active release and locked Hexium packages. | planned |
| `pending-manifest` | full-stack | Validate and queue a mod manifest for maintenance. | planned |
| `mod-config-editor` | full-stack | Edit and queue managed BepInEx configuration overrides. | planned |
| `update-request` | full-stack | Submit a confirmed immediate maintenance request through the scheduled-maintenance workflow. | planned |
| `rollback-request` | full-stack | Submit a confirmed known-good rollback request through a scoped helper. | planned |

## Workspace commands

- **Test dashboard backend**: run Python unit tests.
- **Run dashboard backend**: start the localhost-only API harness.
- **Run dashboard frontend**: start Vite and its development `/api` proxy.
- **Build dashboard frontend**: produce a production React build.
- **Validate dashboard workspace**: run backend tests, frontend lint, and
  frontend build as one command.

Do not start persistent dashboard services while developing this harness.
