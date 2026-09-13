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
backup-inventory publisher, and writes the initial status export.

After both deployments succeed, enable the server and scheduled maintenance:

```bash
make enable-server
sudo systemctl status valheim.service --no-pager
sudo systemctl list-timers --all valheim-restart.timer
```

The timer runs at 00:00 and 12:00 in `Asia/Shanghai`. Its maintenance helper
stops Valheim gracefully, validates the SteamCMD install, stages and activates
the reviewed modpack, then accepts only a new `Game server connected` marker
before declaring success. It rolls the modpack back and restarts vanilla if an
activation fails.

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
| [`systemd/server/valheim-restart.timer`](../systemd/server/valheim-restart.timer) | `/etc/systemd/system/valheim-restart.timer` | Twice-daily maintenance schedule. |
| [`systemd/server/valheim-world-restore-recovery.service`](../systemd/server/valheim-world-restore-recovery.service) | `/etc/systemd/system/valheim-world-restore-recovery.service` | Restores an interrupted world transaction before startup. |
| [`systemd/dashboard/valheim-dashboard.service`](../systemd/dashboard/valheim-dashboard.service) | `/etc/systemd/system/valheim-dashboard.service` | Loopback-only dashboard service. |
| [`systemd/modpack/`](../systemd/modpack) | `/etc/systemd/system/` | Dashboard action and backup-inventory path/service units. |

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
