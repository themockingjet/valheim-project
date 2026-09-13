"""Bounded validation and loading for root-exported Valheim world backups."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

MAX_INVENTORY_BYTES = 64 * 1024
MAX_BACKUPS = 50
BACKUP_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{22,64}\Z")


class WorldBackupError(Exception):
    """A root-owned world-backup export cannot be exposed safely."""

    code = "world_backups_invalid"


class WorldBackupUnavailable(WorldBackupError):
    """The root exporter has not published backup inventory."""

    code = "world_backups_unavailable"


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorldBackupError(f"{name} must be an object")
    return value


def _require_keys(mapping: dict[str, Any], name: str, keys: set[str]) -> None:
    if set(mapping) != keys:
        raise WorldBackupError(f"{name} has an unsupported shape")


def validate_inventory(value: object) -> dict[str, Any]:
    """Validate the fixed root-exported inventory before serving it."""

    inventory = _require_mapping(value, "world backup inventory")
    _require_keys(
        inventory,
        "world backup inventory",
        {"schema_version", "generated_at", "active_world", "native_retention", "backups"},
    )
    if inventory["schema_version"] != 1 or not _is_timestamp(inventory["generated_at"]):
        raise WorldBackupError("world backup inventory metadata is invalid")

    active = _require_mapping(inventory["active_world"], "active_world")
    _require_keys(active, "active_world", {"file_count", "size_bytes", "integrity"})
    if (
        not _is_nonnegative_int(active["file_count"])
        or not _is_nonnegative_int(active["size_bytes"])
        or active["integrity"] not in {"ready", "unavailable"}
    ):
        raise WorldBackupError("active world metadata is invalid")

    retention = _require_mapping(inventory["native_retention"], "native_retention")
    _require_keys(retention, "native_retention", {"observed_count"})
    if not _is_nonnegative_int(retention["observed_count"]):
        raise WorldBackupError("native retention metadata is invalid")

    backups = inventory["backups"]
    if not isinstance(backups, list) or len(backups) > MAX_BACKUPS:
        raise WorldBackupError("world backups are invalid")
    for backup in backups:
        entry = _require_mapping(backup, "world backup")
        _require_keys(
            entry,
            "world backup",
            {"id", "kind", "created_at", "size_bytes", "file_count", "integrity"},
        )
        if (
            not isinstance(entry["id"], str)
            or BACKUP_ID_PATTERN.fullmatch(entry["id"]) is None
            or entry["kind"] != "native-auto"
            or not _is_timestamp(entry["created_at"])
            or not _is_nonnegative_int(entry["size_bytes"])
            or not _is_nonnegative_int(entry["file_count"])
            or entry["integrity"] != "ready"
        ):
            raise WorldBackupError("world backup metadata is invalid")
    return inventory


def load_inventory(path: Path) -> dict[str, Any]:
    """Load one bounded root-written backup inventory file."""

    try:
        with path.open("rb") as inventory_file:
            file_status = os.fstat(inventory_file.fileno())
            if not stat.S_ISREG(file_status.st_mode):
                raise WorldBackupError("world backup inventory is not a regular file")
            if file_status.st_size > MAX_INVENTORY_BYTES:
                raise WorldBackupError("world backup inventory exceeds the maximum size")
            contents = inventory_file.read(MAX_INVENTORY_BYTES + 1)
    except FileNotFoundError as error:
        raise WorldBackupUnavailable("world backup inventory is not available yet") from error
    except OSError as error:
        raise WorldBackupUnavailable("world backup inventory cannot be read") from error
    if len(contents) > MAX_INVENTORY_BYTES:
        raise WorldBackupError("world backup inventory exceeds the maximum size")
    try:
        return validate_inventory(json.loads(contents.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise WorldBackupError("world backup inventory is not valid JSON") from error
