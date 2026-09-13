# Valheim Deployment Kit

This repository is the deployment kit for a Valheim dedicated server, its
Hexium-only modpack, and its localhost-only operations dashboard.

| Directory | Contents |
| --- | --- |
| [`scripts/`](./scripts) | Executable server, dashboard, and modpack installers/helpers. |
| [`systemd/`](./systemd) | Canonical service, timer, path-unit, and drop-in templates. |
| [`config/`](./config) | Reviewed, non-secret defaults. |
| [`valheim-dashboard/`](./valheim-dashboard) | Localhost-only Python/React dashboard source. |
| [`valheim-modpack/`](./valheim-modpack) | Hexium-only resolver, release, and maintenance source. |

Start with the [server deployment guide](./docs/VALHEIM_SERVER_GUIDE.md). It
defines the required host prerequisites, Make command workflow, deployment
order, security boundaries, and operational directories.

The deployed state remains outside this repository:

- `/opt/valheim/` contains the dedicated server and modpack runtime.
- `/opt/valheim-dashboard/` contains immutable dashboard releases.
- `/var/lib/valheim-dashboard/` contains restricted dashboard state.
