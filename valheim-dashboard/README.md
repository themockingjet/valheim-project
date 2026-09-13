# Valheim Dashboard

Localhost-only operations dashboard for the Valheim server at `/opt/valheim`.

This repository isolates the two application layers:

- `backend/` is the Python control-plane API. It remains responsible for
  enforcing localhost-only access and will later mediate all scoped
  operational actions.
- `frontend/` is the Vite + React operator interface. It communicates only
  with the backend API and contains no Valheim filesystem or systemd access.

Neither project may contain Valheim worlds, server credentials, Hexium
archives, generated production manifests, logs, or runtime state. The backend
commits its `uv.lock` dependency lockfile.

## Development

Feature lifecycle and VS Code/Copilot development guidance are in
[`docs/development-harness.md`](./docs/development-harness.md) and
[`.github/copilot-instructions.md`](./.github/copilot-instructions.md).

The backend uses [Astral uv](https://docs.astral.sh/uv/) and the installed
system Python 3.14. Bootstrap its environment once (and again after dependency
changes):

```sh
cd backend
uv sync
```

This creates the ignored `backend/.venv/` for the service's dependencies. Do
not activate the environment manually or set `PYTHONPATH`; use `uv run` for
backend commands. Commit `uv.lock` whenever the backend's dependency metadata
changes.

Run backend tests:

```sh
cd backend
uv run python -m unittest discover -s tests -v
```

Run the backend health endpoint:

```sh
cd backend
uv run python main.py
```

Run the Vite React frontend in another terminal:

```sh
cd frontend
npm run dev
```

Vite proxies `/api/*` requests to the backend, so visit its displayed local
URL (normally `http://localhost:5173`). The backend health route is available
in development at `/api/healthz`; direct backend access remains
`http://127.0.0.1:8080/healthz`.

## Intended production layout

Source is developed here and deployed as a root-owned release under
`/opt/valheim-dashboard/releases/<git-sha>/`, activated through the
`/opt/valheim-dashboard/current` symlink. Mutable UI state belongs under
`/var/lib/valheim-dashboard/`; ephemeral runtime state belongs under
`/run/valheim-dashboard/`.

The root-run [`scripts/dashboard/valheim-dashboard-deploy`](../scripts/dashboard/valheim-dashboard-deploy)
script validates the locked backend environment and tests, builds the Vite
frontend, creates an immutable release, atomically updates `current`, and
starts the localhost-only `valheim-dashboard.service`. It creates the
restricted `valheim-ui` service account and state directories only during
that deployment.

For traceability and rollback safety, deployment requires a clean committed
Git revision. Until this repository has its first commit, the script exits
before creating `/opt/valheim-dashboard/`, `/var/lib/valheim-dashboard/`, the
service account, or the systemd unit.

## Configuration

The current dashboard does not load a `.env` file. Its systemd unit supplies
the compiled static-directory path; the listener remains fixed to loopback and
uses its default exact Host policy. Server credentials belong only in the
host-owned Valheim startup script, never in dashboard frontend assets.

## Scope

The dashboard will:

- display exported server and maintenance status, refreshing the validated
  snapshot only when the fixed same-origin SSE change signal observes a new
  root-exported snapshot;
- seed the editable manifest from the current declared packages (or an already
  queued manifest), then support explicit package additions, removals, and
  bounded active-version selection before queueing the complete resulting
  manifest; resolver-managed dependencies remain read-only;
- validate and queue Hexium manifest changes for the next maintenance window;
- offer an explicitly confirmed update-and-restart action that runs the same
  maintenance workflow immediately;
- offer confirmed rollback requests through scoped helpers.

The frontend and backend will not directly modify `/opt/valheim/server`,
`/opt/valheim/data`, or the active modpack deployment.
