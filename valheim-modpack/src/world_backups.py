"""Root-side inventory and transactional restore support for Valheim worlds.

This module never receives a caller-controlled filesystem path.  Its
production defaults point only to Valheim's fixed worlds directory and its
root-owned state.  The dashboard receives an opaque ID, not a directory name.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any

DEFAULT_WORLDS_DIRECTORY = Path("/opt/valheim/data/worlds_local")
DEFAULT_STATE_DIRECTORY = Path("/var/lib/valheim-world-backups")
DEFAULT_INDEX_PATH = DEFAULT_STATE_DIRECTORY / "backup-index.json"
DEFAULT_EXPORT_PATH = Path("/var/lib/valheim-dashboard/exports/world-backups.json")
DEFAULT_TRANSACTION_DIRECTORY = Path("/opt/valheim/data/.dashboard-world-restores")

MAX_BACKUPS = 50
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024 * 1024
BACKUP_NAME = re.compile(r"^.+_backup_auto-\d{8}-\d{6}\Z")
BACKUP_ID = re.compile(r"^[A-Za-z0-9_-]{22,64}\Z")
MAIN_DB = re.compile(r"^_main\.\d+\.db2\Z")
MAIN_FWL = re.compile(r"^_main\.\d+\.fwl2\Z")
MAIN_OK = re.compile(r"^_main\.\d+\.ok\Z")


class WorldBackupError(Exception):
    """A fixed world-backup operation cannot proceed safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object, *, mode: int, group_id: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        if group_id is not None:
            os.fchown(descriptor, 0, group_id)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _snapshot_metadata(path: Path) -> tuple[int, int]:
    """Validate a complete, non-symlinked, one-directory Valheim snapshot."""

    try:
        path_status = path.lstat()
    except OSError as error:
        raise WorldBackupError("world snapshot is unavailable") from error
    if not stat.S_ISDIR(path_status.st_mode) or stat.S_ISLNK(path_status.st_mode):
        raise WorldBackupError("world snapshot is not a directory")

    files = list(path.iterdir())
    if not files or len(files) > MAX_SNAPSHOT_FILES:
        raise WorldBackupError("world snapshot file count is invalid")
    byte_count = 0
    db_count = fwl_count = ok_count = chunk_count = 0
    for entry in files:
        entry_status = entry.lstat()
        if stat.S_ISLNK(entry_status.st_mode) or not stat.S_ISREG(entry_status.st_mode):
            raise WorldBackupError("world snapshot contains an unsafe entry")
        byte_count += entry_status.st_size
        if byte_count > MAX_SNAPSHOT_BYTES:
            raise WorldBackupError("world snapshot exceeds the maximum size")
        db_count += bool(MAIN_DB.fullmatch(entry.name))
        fwl_count += bool(MAIN_FWL.fullmatch(entry.name))
        ok_count += bool(MAIN_OK.fullmatch(entry.name))
        chunk_count += entry.name.endswith(".chunk")
    if db_count != 1 or fwl_count != 1 or ok_count != 1 or not chunk_count:
        raise WorldBackupError("world snapshot is incomplete")
    return len(files), byte_count


def _active_world(worlds_directory: Path) -> Path | None:
    try:
        candidates = [
            entry
            for entry in worlds_directory.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and not entry.name.startswith(".")
            and BACKUP_NAME.fullmatch(entry.name) is None
        ]
    except OSError as error:
        raise WorldBackupError("world directory is unavailable") from error
    if not candidates:
        return None
    if len(candidates) > 1:
        raise WorldBackupError("exactly one active world directory is required")
    _snapshot_metadata(candidates[0])
    return candidates[0]


def _native_backups(worlds_directory: Path) -> list[Path]:
    try:
        candidates = [
            entry
            for entry in worlds_directory.iterdir()
            if entry.is_dir()
            and not entry.is_symlink()
            and BACKUP_NAME.fullmatch(entry.name) is not None
        ]
    except OSError as error:
        raise WorldBackupError("world backup directory is unavailable") from error
    candidates.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    if len(candidates) > MAX_BACKUPS:
        candidates = candidates[:MAX_BACKUPS]
    for backup in candidates:
        _snapshot_metadata(backup)
    return candidates


def _load_index(path: Path) -> dict[str, dict[str, int | str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict) or len(entries) > MAX_BACKUPS:
        return {}
    accepted: dict[str, dict[str, int | str]] = {}
    for backup_id, entry in entries.items():
        if (
            not isinstance(backup_id, str)
            or BACKUP_ID.fullmatch(backup_id) is None
            or not isinstance(entry, dict)
            or set(entry) != {"name", "device", "inode"}
            or not isinstance(entry["name"], str)
            or BACKUP_NAME.fullmatch(entry["name"]) is None
            or not isinstance(entry["device"], int)
            or not isinstance(entry["inode"], int)
        ):
            return {}
        accepted[backup_id] = entry
    return accepted


def _new_backup_id(entries: dict[str, dict[str, int | str]]) -> str:
    while True:
        backup_id = secrets.token_urlsafe(18)
        if backup_id not in entries:
            return backup_id


def _unavailable_inventory() -> tuple[
    dict[str, Any], dict[str, Path], dict[str, dict[str, int | str]]
]:
    return (
        {
            "schema_version": 1,
            "generated_at": _now(),
            "active_world": {
                "file_count": 0,
                "size_bytes": 0,
                "integrity": "unavailable",
            },
            "native_retention": {"observed_count": 0},
            "backups": [],
        },
        {},
        {},
    )


def _inventory(
    worlds_directory: Path, index_path: Path
) -> tuple[dict[str, Any], dict[str, Path], dict[str, dict[str, int | str]]]:
    try:
        worlds_status = worlds_directory.lstat()
    except FileNotFoundError:
        return _unavailable_inventory()
    except OSError as error:
        raise WorldBackupError("world directory is unavailable") from error
    if not stat.S_ISDIR(worlds_status.st_mode) or stat.S_ISLNK(worlds_status.st_mode):
        raise WorldBackupError("world directory is unsafe")

    try:
        active = _active_world(worlds_directory)
    except WorldBackupError:
        return _unavailable_inventory()
    if active is None:
        return _unavailable_inventory()
    active_files, active_bytes = _snapshot_metadata(active)
    prior_index = _load_index(index_path)
    next_index: dict[str, dict[str, int | str]] = {}
    paths: dict[str, Path] = {}
    entries: list[dict[str, Any]] = []
    for backup in _native_backups(worlds_directory):
        backup_status = backup.stat()
        file_count, size_bytes = _snapshot_metadata(backup)
        identity = {
            "name": backup.name,
            "device": backup_status.st_dev,
            "inode": backup_status.st_ino,
        }
        backup_id = next(
            (
                existing_id
                for existing_id, existing in prior_index.items()
                if existing == identity
            ),
            _new_backup_id(next_index),
        )
        next_index[backup_id] = identity
        paths[backup_id] = backup
        entries.append(
            {
                "id": backup_id,
                "kind": "native-auto",
                "created_at": _timestamp(backup_status.st_mtime),
                "size_bytes": size_bytes,
                "file_count": file_count,
                "integrity": "ready",
            }
        )
    return (
        {
            "schema_version": 1,
            "generated_at": _now(),
            "active_world": {
                "file_count": active_files,
                "size_bytes": active_bytes,
                "integrity": "ready",
            },
            "native_retention": {"observed_count": len(entries)},
            "backups": entries,
        },
        paths,
        next_index,
    )


def publish_inventory(
    *,
    worlds_directory: Path = DEFAULT_WORLDS_DIRECTORY,
    index_path: Path = DEFAULT_INDEX_PATH,
    output_path: Path = DEFAULT_EXPORT_PATH,
    dashboard_group_id: int | None = None,
) -> dict[str, Any]:
    """Refresh the opaque root index and atomically publish dashboard metadata."""

    inventory, _paths, index = _inventory(worlds_directory, index_path)
    if output_path == DEFAULT_EXPORT_PATH and dashboard_group_id is None:
        dashboard_group_id = grp.getgrnam("valheim-ui").gr_gid
    _atomic_json(index_path, {"schema_version": 1, "entries": index}, mode=0o600)
    _atomic_json(output_path, inventory, mode=0o640, group_id=dashboard_group_id)
    return inventory


def _transaction_path(transaction_directory: Path, transaction_id: str) -> Path:
    if BACKUP_ID.fullmatch(transaction_id) is None:
        raise WorldBackupError("restore transaction is invalid")
    candidate = transaction_directory / transaction_id
    if candidate.parent != transaction_directory:
        raise WorldBackupError("restore transaction is invalid")
    return candidate


def _load_transaction(path: Path) -> dict[str, str]:
    try:
        value = json.loads((path / "transaction.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise WorldBackupError("restore transaction is unavailable") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "transaction_id", "active_name", "phase"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("transaction_id"), str)
        or not isinstance(value.get("active_name"), str)
        or value.get("phase") not in {"staged", "active_moved", "restored", "completed", "recovered"}
    ):
        raise WorldBackupError("restore transaction is invalid")
    return value


def _write_transaction(path: Path, transaction_id: str, active_name: str, phase: str) -> None:
    _atomic_json(
        path / "transaction.json",
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "active_name": active_name,
            "phase": phase,
        },
        mode=0o600,
    )


def _set_ownership(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for entry in path.iterdir():
        os.chown(entry, uid, gid)


def prepare_restore(
    backup_id: str,
    *,
    worlds_directory: Path = DEFAULT_WORLDS_DIRECTORY,
    index_path: Path = DEFAULT_INDEX_PATH,
    transaction_directory: Path = DEFAULT_TRANSACTION_DIRECTORY,
) -> str:
    """Stage a native backup and atomically make it active while server is stopped."""

    inventory, paths, index = _inventory(worlds_directory, index_path)
    _atomic_json(index_path, {"schema_version": 1, "entries": index}, mode=0o600)
    if backup_id not in paths:
        raise WorldBackupError("selected world backup is no longer available")
    source = paths[backup_id]
    active = _active_world(worlds_directory)
    active_status = active.stat()
    transaction_id = _new_backup_id(index)
    transaction = _transaction_path(transaction_directory, transaction_id)
    transaction.mkdir(parents=True, mode=0o700)
    staged = transaction / "staged"
    previous = transaction / "previous-active"
    shutil.copytree(source, staged, copy_function=shutil.copy2)
    _set_ownership(staged, active_status.st_uid, active_status.st_gid)
    _snapshot_metadata(staged)
    _write_transaction(transaction, transaction_id, active.name, "staged")
    os.replace(active, previous)
    _fsync_directory(worlds_directory)
    _write_transaction(transaction, transaction_id, active.name, "active_moved")
    os.replace(staged, active)
    _fsync_directory(worlds_directory)
    _write_transaction(transaction, transaction_id, active.name, "restored")
    return transaction_id


def complete_restore(transaction_id: str, *, transaction_directory: Path = DEFAULT_TRANSACTION_DIRECTORY) -> None:
    transaction = _transaction_path(transaction_directory, transaction_id)
    record = _load_transaction(transaction)
    if record["phase"] != "restored":
        raise WorldBackupError("restore cannot be completed")
    _write_transaction(transaction, transaction_id, record["active_name"], "completed")


def rollback_restore(
    transaction_id: str,
    *,
    worlds_directory: Path = DEFAULT_WORLDS_DIRECTORY,
    transaction_directory: Path = DEFAULT_TRANSACTION_DIRECTORY,
) -> None:
    """Restore the moved pre-restore world after a failed health check."""

    transaction = _transaction_path(transaction_directory, transaction_id)
    record = _load_transaction(transaction)
    if record["phase"] not in {"active_moved", "restored"}:
        raise WorldBackupError("restore cannot be rolled back")
    active = worlds_directory / record["active_name"]
    previous = transaction / "previous-active"
    failed = transaction / "failed-restored"
    if not previous.is_dir() or previous.is_symlink():
        raise WorldBackupError("pre-restore world is unavailable")
    _snapshot_metadata(previous)
    if active.exists():
        _snapshot_metadata(active)
        os.replace(active, failed)
    os.replace(previous, active)
    _fsync_directory(worlds_directory)
    _write_transaction(transaction, transaction_id, record["active_name"], "recovered")


def recover_incomplete_restores(
    *,
    worlds_directory: Path = DEFAULT_WORLDS_DIRECTORY,
    transaction_directory: Path = DEFAULT_TRANSACTION_DIRECTORY,
) -> list[str]:
    """Recover unfinished restores before a server start can use partial state."""

    if not transaction_directory.exists():
        return []
    recovered: list[str] = []
    for transaction in transaction_directory.iterdir():
        if not transaction.is_dir() or transaction.is_symlink() or BACKUP_ID.fullmatch(transaction.name) is None:
            continue
        record = _load_transaction(transaction)
        if record["phase"] in {"active_moved", "restored"}:
            rollback_restore(
                record["transaction_id"],
                worlds_directory=worlds_directory,
                transaction_directory=transaction_directory,
            )
            recovered.append(record["transaction_id"])
    return recovered


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worlds-directory", type=Path, default=DEFAULT_WORLDS_DIRECTORY)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTION_DIRECTORY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--output", type=Path, default=DEFAULT_EXPORT_PATH)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--backup-id", required=True)
    complete = subparsers.add_parser("complete")
    complete.add_argument("--transaction-id", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--transaction-id", required=True)
    subparsers.add_parser("recover")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "inventory":
            result = publish_inventory(
                worlds_directory=arguments.worlds_directory,
                index_path=arguments.index,
                output_path=arguments.output,
            )
        elif arguments.command == "prepare":
            result = {
                "transaction_id": prepare_restore(
                    arguments.backup_id,
                    worlds_directory=arguments.worlds_directory,
                    index_path=arguments.index,
                    transaction_directory=arguments.transactions,
                )
            }
        elif arguments.command == "complete":
            complete_restore(arguments.transaction_id, transaction_directory=arguments.transactions)
            result = {"outcome": "completed"}
        elif arguments.command == "rollback":
            rollback_restore(
                arguments.transaction_id,
                worlds_directory=arguments.worlds_directory,
                transaction_directory=arguments.transactions,
            )
            result = {"outcome": "recovered"}
        else:
            result = {
                "recovered": recover_incomplete_restores(
                    worlds_directory=arguments.worlds_directory,
                    transaction_directory=arguments.transactions,
                )
            }
    except WorldBackupError:
        print(json.dumps({"outcome": "rejected"}))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
