# Valheim Deployment Kit

This repository provisions and deploys one localhost-only Valheim operations
stack on Ubuntu 22.04 or later. It intentionally does not commit server data,
credentials, downloaded archives, runtime state, generated locks, or world
backups.

The deployment sequence is fixed:

1. Provision the dedicated-server host and install inactive base units.
2. Deploy the dashboard to create `valheim-ui` and its state directories.
3. Deploy the modpack helpers, exporters, and action watcher definitions.
4. Enable the Valheim service and scheduled maintenance timer.
5. Optionally enable dashboard-initiated privileged actions.

Do not start Valheim before steps 1-3 have completed. The server's recovery
unit and maintenance service require the modpack helpers installed in step 3.
`make bootstrap` refuses to run when `valheim.service` is active, so stop the
server and take an independent backup before using it on an existing host. It
deliberately leaves Valheim disabled until its host-owned startup script has
been created and tested.

## Host prerequisites

Install SteamCMD and the Valheim runtime dependencies:

```bash
make server-prerequisites
```

Permit the documented Steam-backend UDP ports in both UFW and the provider
firewall/security group:

```bash
make server-firewall
```

## Provision the server

From the repository root, run:

```bash
make provision-server
```

Provisioning creates the `valheim` account plus `/opt/valheim/server`,
`/opt/valheim/data`, and `/opt/valheim/logs`; installs/updates Steam app
`896660`; installs the BepInEx-aware launcher; and copies the inactive base
systemd templates. It does not start the server or enable its timer.

For an already-running server migrated from an earlier version of this kit,
run `make migrate-systemd`. It installs canonical unit files and removes only
the kit's prior drop-ins/action watchers, reloads systemd, and does not run
SteamCMD or restart `valheim.service`. It refuses migration while a dashboard
request or legacy action helper is active. The updated Valheim unit applies on
the next normal restart.

Before enabling the server, create and test
`/opt/valheim/server/start_valheim_server.sh` as the `valheim` user. This
host-owned script is the authoritative Valheim command and contains the server
name, world, password, port, public/crossplay choice, and any world modifiers.
Keep it out of Git. The deployment kit's launcher only adds optional BepInEx
environment variables before it executes that script.

## Deploy dashboard and modpack

Deploy the dashboard before the modpack:

```bash
make deploy
```

The dashboard deployer creates the restricted `valheim-ui` account and the
shared state hierarchy. The modpack deployer requires that account, installs
the root helpers, seeds `/opt/valheim/modpack/manifest.yaml` only when absent,
creates its writable `config-overrides` directory, starts the non-disruptive
backup-inventory publisher, writes the initial status export, and enables the
root-owned one-minute status publisher timer. This keeps the dashboard
snapshot within its five-minute freshness window without granting the
dashboard service access to systemd or server files.

It also seeds
`/opt/valheim/modpack/config-overrides/io.hexium.valheim.lifecycleannouncer.cfg`
when absent. The override sets the Lifecycle Announcer socket to
`/run/valheim/lifecycle-announcer.sock`; retain that absolute path unless the
matching `valheim.service` runtime-directory configuration is changed.

After both deployments succeed, enable the server and scheduled maintenance:

```bash
make enable-server
sudo systemctl status valheim.service --no-pager
sudo systemctl list-timers --all valheim-restart.timer
```

The timer begins the scheduled restart announcement phase at 23:45 and 11:45
in `Asia/Shanghai`. Players receive notices 15, 10, 5, 3, and 1 minutes before
maintenance starts at 00:00 and 12:00. Its maintenance helper then stops
Valheim, validates the SteamCMD install, stages and activates the reviewed
modpack, and accepts only a new `Game server connected` marker before declaring
success. It rolls the modpack back and restarts vanilla if an activation fails.
Manual, dashboard-initiated, rollback, and restore stops are not delayed by
this scheduled countdown.

## Optional dashboard actions

The dashboard can always display root-exported status. It cannot perform
privileged actions until explicitly authorized:

```bash
make enable-dashboard-actions
```

This command refuses to enable the watcher while a request is pending. The
single `valheim-dashboard-actions.path` watcher serializes the four fixed
manifest, restart, rollback, and world-restore requests. Each root helper
re-validates its request and writes only a bounded result for `valheim-ui` to
read.

## Installed systemd templates

| Source | Installed destination | Purpose |
| --- | --- | --- |
| [`systemd/server/valheim.service`](../systemd/server/valheim.service) | `/etc/systemd/system/valheim.service` | Dedicated server, recovery dependency, BepInEx-aware launcher, and hardened writable paths. |
| [`systemd/server/valheim-restart.service`](../systemd/server/valheim-restart.service) | `/etc/systemd/system/valheim-restart.service` | Scheduled modpack maintenance. |
| [`systemd/server/valheim-restart-announcement.service`](../systemd/server/valheim-restart-announcement.service) | `/etc/systemd/system/valheim-restart-announcement.service` | Scheduled-only 15-minute player warning sequence. |
| [`systemd/server/valheim-restart.timer`](../systemd/server/valheim-restart.timer) | `/etc/systemd/system/valheim-restart.timer` | Twice-daily maintenance schedule. |
| [`systemd/server/valheim-world-restore-recovery.service`](../systemd/server/valheim-world-restore-recovery.service) | `/etc/systemd/system/valheim-world-restore-recovery.service` | Restores an interrupted world transaction before startup. |
| [`systemd/dashboard/valheim-dashboard.service`](../systemd/dashboard/valheim-dashboard.service) | `/etc/systemd/system/valheim-dashboard.service` | Loopback-only dashboard service. |
| [`systemd/modpack/`](../systemd/modpack) | `/etc/systemd/system/` | Dashboard action, backup-inventory, and one-minute status publisher units. |

## Operational directories

| Directory | Owner and mode | Purpose |
| --- | --- | --- |
| `/opt/valheim/server` | `valheim:valheim`, `0750` | SteamCMD-installed server files. |
| `/opt/valheim/data` | `valheim:valheim`, `0750` | Worlds and access lists. |
| `/opt/valheim/logs` | `valheim:valheim`, `0750` | Dedicated-server log. |
| `/opt/valheim/modpack` | `root:valheim`, `0770` | Manifest, locks, cache, releases, and mutable modpack state. |
| `/opt/valheim-dashboard` | `root:root`, `0755` | Immutable dashboard releases and activation links. |
| `/var/lib/valheim-dashboard/pending` | `valheim-ui:valheim-ui`, `0700` | Dashboard action requests. |
| `/var/lib/valheim-dashboard/{exports,state}` | `root:valheim-ui`, `0750` | Root-written status and action results. |
| `/var/lib/valheim-dashboard/audit` | `valheim-ui:valheim-ui`, `0700` | Dashboard audit log. |

## Common operations

```bash
sudo systemctl status valheim.service --no-pager
sudo systemctl restart valheim.service
sudo journalctl -u valheim.service -n 200 --no-pager -l -o cat
sudo tail -F /opt/valheim/logs/valheim.log
```

For an immediate server update, use the installed
`valheim-restart.service`; do not run SteamCMD against a live server manually.
