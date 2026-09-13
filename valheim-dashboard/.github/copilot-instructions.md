# Valheim Dashboard Copilot Instructions

## Repository boundaries

- `backend/` is the Python control-plane API.
- `frontend/` is the Vite + React operator UI.
- `docs/` contains requirements, architecture decisions, and feature tracking.
- Do not add application code at the repository root.
- Do not add Valheim worlds, credentials, downloaded Hexium archives, logs,
  generated production manifests, production lock files, or runtime state to
  this repository. Commit the backend `uv.lock` dependency lockfile.

## Security and operational constraints

- Keep the production dashboard localhost-only. Do not bind the backend to a
  non-loopback address or add a public listener.
- The frontend must not directly access the Valheim filesystem, invoke
  `systemctl`, or execute commands. It uses documented backend `/api/` routes.
- The backend must not directly modify `/opt/valheim/server`,
  `/opt/valheim/data`, or active mod deployments.
- State-changing routes require strict input validation, CSRF protection,
  Origin/Host checks, explicit confirmation, and audit logging.
- Hexium is the only mod source. Do not add Thunderstore endpoints.
- Pending changes are queued for the existing maintenance boundary; do not
  introduce immediate browser-initiated mod deployments.

## Feature workflow

1. Find or create the feature in `docs/feature-tracker.md`.
2. Define the backend contract before adding a frontend integration.
3. Implement backend validation and tests first.
4. Implement the React UI against that contract.
5. Run the smallest applicable backend tests and frontend lint/build checks.
6. Update the feature tracker only after verification succeeds.

## Backend conventions

- The backend uses Astral `uv` with system Python 3.14. Run `uv sync` from
  `backend/` to create or update `backend/.venv/`; never commit `.venv/`.
- Run backend commands through `uv run`; do not activate `.venv` manually or
  use `PYTHONPATH`. Start the service with `uv run python main.py` from
  `backend/`.
- Declare every dependency in `backend/pyproject.toml` and update/commit
  `backend/uv.lock` whenever that metadata changes.
- Use Python standard-library facilities unless an approved feature requires a
  dependency.
- Keep API payloads JSON and bounded. Read operational data from explicit,
  atomically-written export files rather than unbounded journal streams.
- Return intentional HTTP errors with safe, actionable messages. Do not hide
  failures through broad exception handling or success-shaped fallbacks.
- Add unit tests for each route, validation rule, and action boundary.

## Frontend conventions

- Use Vite + React in `frontend/`.
- During development, use the Vite `/api` proxy; do not hardcode a host, VPS
  address, or port in React code.
- Treat all operational data as untrusted: render it safely and show loading,
  empty, and error states.
- Keep feature components focused and avoid adding global state until a
  concrete feature needs it.

## Validation commands

Run from the repository root:

```sh
cd backend && uv sync --locked && uv run python -m unittest discover -s tests -v
cd frontend && npm run lint && npm run build
```

Use the `Validate dashboard workspace` VS Code task when a change spans both
projects.
