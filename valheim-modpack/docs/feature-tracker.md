# Feature Tracker

## Resolver foundation

- [x] Hexium-only YAML manifest schema.
- [x] Exact-version metadata checks and dependency traversal.
- [x] Stable-by-default prerelease rejection.
- [x] Archive cache with ZIP validation and SHA-256 lock values.
- [x] Atomic lock publication.
- [x] Deploy staged BepInEx/plugin release safely.
- [x] Activate releases atomically and retain rollback targets.
- [x] Add non-destructive plan, status, and cache/release retention commands.
- [x] Install launcher and maintenance-service integration without changing the
  existing timer cadence.
- [x] Validate a complete authorized maintenance activation, BepInEx startup,
  and `Game server connected` health check (2026-09-13 01:54-01:56 UTC+8).
- [ ] Validate the next scheduled maintenance-timer activation.
- [x] Rehearse atomic rollback to a prior known-good release and confirm the
  restarted server reaches `Game server connected` (2026-09-13 04:35-04:36
  UTC+8).

## Dashboard-triggered root helpers (B2/B5/B6 source)

- [x] `apply_pending_manifest()` independently re-validates a strict fixed
  request schema, reuses the authoritative manifest loader/resolver, diffs
  against the existing lock for a bounded preview, and atomically replaces
  `manifest.yaml` without ever touching `valheim.service` or activating a
  release (13 unit tests, `python3 -m unittest tests.test_manifest_apply`).
- [x] `apply-pending-manifest` CLI subcommand wired through the same
  `update.lock` concurrency guard as `resolve`/`update`/`rollback` (2 CLI
  parser tests).
- [x] `valheim-manifest-apply`, `valheim-restart-request`, and
  `valheim-rollback-request` root wrapper scripts implemented: independent
  schema re-validation, bounded result files only (no raw stdout/stderr/
  exception data), consumed-request cleanup on every path, and a shared
  15-minute restart cooldown for the two helpers capable of restarting
  Valheim. Installed with their path watchers enabled (2026-09-13); no
  request was pending or submitted during installation, so Valheim was not
  restarted.
- [ ] Live-authorized rehearsal of the manifest-apply helper against the real
  pending directory and `/opt/valheim/modpack`.
- [ ] Live-authorized rehearsal of the restart-only and rollback helpers,
  including their cooldown and health check, against the running server.

## World backup management (B7 source)

- [x] Root-side `world_backups.py` inventories only fixed native
  `worlds_local` directories, validates complete non-symlinked chunked-world
  structures, writes a root-owned opaque-ID index, and atomically exports
  bounded metadata without world paths/content (6 scratch-directory tests).
- [x] Transactional whole-directory restore source stages the chosen indexed
  native backup, atomically preserves the pre-restore active world, promotes
  the staging directory, and supports explicit completion, rollback, and
  interrupted-restore recovery before a server start. It also shares the
  fixed 15-minute restart cooldown with the other restart-capable helpers.
- [x] Reproducible inventory/restore/recovery scripts and systemd units added.
  The inventory publisher is non-disruptive; restore stops and starts Valheim
  only after an explicitly confirmed dashboard request.
- [x] Installed the complete dashboard integration (2026-09-13): inventory,
  manifest, restart, rollback, restore, and restore-recovery units are
  installed, and all four request watchers are enabled with an empty pending
  directory. The inventory publishes four bounded, opaque-ID native-backup
  entries. No restore request was submitted and no live world data was changed.
