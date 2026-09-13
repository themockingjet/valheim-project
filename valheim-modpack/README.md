# Valheim Modpack

Hexium-only, version-pinned mod management for the dedicated Valheim server.

This repository is the source for the executable deployed at
`/opt/valheim/modpack/bin/valheim-modpack`. It does not contain world data,
downloaded archives, production locks, or credentials.

## Initial resolver milestone

The `resolve` command reads a reviewed YAML manifest, resolves its complete
dependency graph through `https://valheim.hexium.gg`, downloads exact package
archives into a local cache, verifies them as readable ZIPs, calculates a
SHA-256 for each, and atomically writes a generated JSON lock file.

Only `source: hexium` is accepted. A resolution fails on malformed manifests,
inactive/deprecated packages, prereleases unless explicitly allowed, conflicts,
or a dependency whose required minimum version is not met.

```sh
python3 main.py resolve \
  --manifest example-manifest.yaml \
  --lock /tmp/modpack.lock.json \
  --cache /tmp/valheim-modpack-cache
```

Run tests:

```sh
python3 -m unittest discover -s tests -v
```

## Live-server installation status

The first deployed manifest contains only the exact, tested
`denikson-BepInExPack_Valheim` version. Resolving/staging it does not modify
the running server or activate BepInEx. Activation is guarded: the CLI rejects
it while `valheim.service` is running and is reserved for the maintenance
orchestration milestone.

## Installed operator commands

The installed executable is `/opt/valheim/modpack/bin/valheim-modpack`.

```sh
# Resolve the active reviewed manifest and write its checksummed lock.
sudo -u valheim /opt/valheim/modpack/bin/valheim-modpack resolve

# Resolve in temporary storage and compare the candidate with the active lock.
sudo -u valheim /opt/valheim/modpack/bin/valheim-modpack plan

# Show the active staged release.
sudo -u valheim /opt/valheim/modpack/bin/valheim-modpack status

# Stage without changing the active deployment.
sudo -u valheim /opt/valheim/modpack/bin/valheim-modpack stage

# Prune only unreferenced cache entries and releases beyond retention.
sudo -u valheim /opt/valheim/modpack/bin/valheim-modpack gc --keep 3
```

`update` and `rollback` deliberately fail while `valheim.service` is active.
The root-owned maintenance orchestrator is the only normal activation path:
it stops Valheim, performs SteamCMD validation, runs the atomic `maintain`
operation as `valheim`, starts the service through the BepInEx-aware launcher,
and waits up to 180 seconds for `Game server connected`. On a failed
update or health check, it restores the previous release; when no prior
release exists, it removes the new active link and restarts vanilla.
The health check only accepts a marker written after the current start,
monitoring both Valheim's logfile and BepInEx's runtime log to handle either
logging path and logfile truncation safely.

The existing `valheim-restart.timer` remains the sole scheduler and retains
its 00:00/12:00 Asia/Shanghai cadence.

## Dashboard-triggered root helpers (fixed directory-job transport)

The unprivileged dashboard backend never touches the filesystem, systemd, or
this modpack directly. Instead it atomically writes an opaque, strictly
schema-validated JSON request into its own `/var/lib/valheim-dashboard/pending`
directory. Four root-owned, systemd-`Path`-triggered helpers in
[`scripts/modpack/`](../scripts/modpack) are the only consumers of that directory, and the only
producers of `/var/lib/valheim-dashboard/state`, which the dashboard may only
read:

| Pending file | Helper script | Unit pair | Effect |
| --- | --- | --- | --- |
| `manifest-request.json` | [`valheim-manifest-apply`](../scripts/modpack/valheim-manifest-apply) | `valheim-dashboard-actions.{path,service}` | Re-validates the request against the same manifest schema as `load_manifest`, confirms it resolves, and atomically replaces `manifest.yaml`. **Never** stops/starts/restarts `valheim.service` or activates a release; it only changes what the next scheduled 00:00/12:00 maintenance run will apply. |
| `update-request.json` | [`valheim-update-request`](../scripts/modpack/valheim-update-request) | `valheim-dashboard-actions.{path,service}` | Starts `valheim-restart.service` immediately. It runs the complete maintenance workflow: validates the install, resolves and activates the current manifest, restarts Valheim, automatically rolls back a failed activation, and waits for `Game server connected`. |
| `rollback-request.json` | [`valheim-rollback-request`](../scripts/modpack/valheim-rollback-request) | `valheim-dashboard-actions.{path,service}` | Stops the server, runs `valheim-modpack rollback --allow-active`, restarts, and waits for the same health marker. Shares the restart helper's cooldown so a rollback and a restart cannot both bypass the limit back-to-back. |
| `world-restore-request.json` | [`valheim-world-restore`](../scripts/modpack/valheim-world-restore) | `valheim-dashboard-actions.{path,service}` | Restores one selected native Valheim backup directory only after a graceful stop, then starts Valheim and verifies the ready marker. The dashboard can submit only a root-issued opaque backup ID, never a world path. A root-owned transaction preserves the pre-restore world and automatically restores it if the selected backup fails health checks. |

Every helper:

- independently re-validates the fixed request schema (never trusts the
  dashboard's validation alone);
- writes only a bounded `{"outcome": ..., "message": ...}` (plus, for the
  manifest helper, a `package_count` integer) result file — never raw
  stdout/stderr, stack traces, archive URLs, or checksums;
- removes the consumed pending request file whether it succeeds or fails, so
  a stuck/invalid request cannot wedge the transport; and
- never restarts Valheim outside of the explicit update, rollback, and restore actions, and
  never changes `valheim-restart.timer`'s 00:00/12:00 Asia/Shanghai cadence.

## World backup inventory and restore (B7)

Valheim 1.0 worlds are directories, not a `.db`/`.fwl` pair. The live
server's native automatic backups are complete sibling directories containing
`*.chunk`, `_main.*.db2`, `_main.*.fwl2`, `_main.*.chunks`, and
`_main.*.ok`. A restore must therefore switch a complete, validated directory;
it must never mix files from different backups.

[`world_backups.py`](./src/world_backups.py) discovers only the fixed
`/opt/valheim/data/worlds_local` location. It validates each eligible native
backup as a non-symlinked directory with exactly one `.db2`, `.fwl2`, and
`.ok` marker plus chunk data. It creates root-issued opaque backup IDs in a
root-only index and publishes only bounded metadata (times, sizes, file
counts, and IDs) to the dashboard. No world path, directory name, or world
content is exported.

The inventory publisher is non-disruptive. The restore helper is not: it
stops and starts `valheim.service`. Its transaction sequence stages a copy of
the selected backup, atomically moves the active directory aside as the
pre-restore recovery point, promotes the staging directory, and waits for a
new `Game server connected` marker. A failed health check restores the moved
pre-restore world. A start-time recovery unit reverts any unconfirmed
transaction after an interrupted restore before Valheim opens the world.
It shares the fixed 15-minute restart cooldown with the modpack-rollback
helper, so it cannot immediately follow a rollback.

The dashboard never accesses `/opt/valheim/data`; it reads
`world-backups.json`, writes only the fixed restore-request file, and receives
only a bounded result. Restore requests require the same session, CSRF, exact
Host/Origin/JSON validation, typed `RESTORE` confirmation, short reason,
audit event, and duplicate-request exclusion as the other privileged actions.

### Reproducible install

Run as root, from a checked-out revision of this repository:

```sh
sudo ./scripts/modpack/valheim-modpack-deploy
```

This installs all source, root helpers, systemd units, recovery guards, and
the non-disruptive world-backup inventory publisher. It does not enable any
dashboard request watcher.

To enable the complete dashboard action surface only after confirming that no
dashboard request is pending:

```sh
sudo ./scripts/modpack/valheim-modpack-deploy --enable-dashboard-actions
```

The deployment command refuses that opt-in if it finds a pending
`*-request.json` file. The enabled `.path` unit watches only the four fixed
pending files, and its paired `Type=oneshot` service serializes their helpers.
