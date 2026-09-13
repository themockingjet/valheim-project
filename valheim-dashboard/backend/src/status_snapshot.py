"""Validation and bounded loading for dashboard operational snapshots."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_PACKAGES = 100
MAX_LOG_ENTRIES = 100
MAX_LOG_ENTRY_CHARACTERS = 500
PACKAGE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class SnapshotError(Exception):
    """Base error for a status snapshot that cannot be served safely."""

    code = "snapshot_invalid"


class SnapshotUnavailable(SnapshotError):
    """The exporter has not published a snapshot yet."""

    code = "snapshot_unavailable"


def _is_text(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _is_nullable_text(value: object, *, limit: int) -> bool:
    return value is None or _is_text(value, limit=limit)


def _is_nullable_nonnegative_integer(value: object) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _is_nullable_nonnegative_number(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _is_timestamp(value: object) -> bool:
    if not _is_text(value, limit=40):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_nullable_timestamp(value: object) -> bool:
    return value is None or _is_timestamp(value)


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{name} must be an object")
    return value


def _require_keys(mapping: dict[str, Any], name: str, keys: set[str]) -> None:
    if set(mapping) != keys:
        raise SnapshotError(f"{name} has an unsupported shape")


def _validate_server(value: object) -> None:
    server = _require_mapping(value, "server")
    _require_keys(
        server,
        "server",
        {
            "active_state",
            "sub_state",
            "pid",
            "uptime_seconds",
            "memory_current_bytes",
            "cpu_usage_ns",
        },
    )
    if not _is_text(server["active_state"], limit=32) or not _is_text(
        server["sub_state"], limit=32
    ):
        raise SnapshotError("server state is invalid")
    for field in ("pid", "memory_current_bytes", "cpu_usage_ns"):
        if not _is_nullable_nonnegative_integer(server[field]):
            raise SnapshotError(f"server {field} is invalid")
    if not _is_nullable_nonnegative_number(server["uptime_seconds"]):
        raise SnapshotError("server uptime_seconds is invalid")


def _validate_maintenance(value: object) -> None:
    maintenance = _require_mapping(value, "maintenance")
    _require_keys(maintenance, "maintenance", {"next_scheduled_at", "last"})
    if not _is_nullable_timestamp(maintenance["next_scheduled_at"]):
        raise SnapshotError("next maintenance time is invalid")
    last = _require_mapping(maintenance["last"], "maintenance.last")
    _require_keys(
        last,
        "maintenance.last",
        {
            "outcome",
            "started_at",
            "completed_at",
            "rollback_performed",
            "failure_reason",
        },
    )
    if last["outcome"] not in {"running", "success", "failed", "rolled_back"}:
        raise SnapshotError("maintenance outcome is invalid")
    if not _is_nullable_timestamp(last["started_at"]) or not _is_nullable_timestamp(
        last["completed_at"]
    ):
        raise SnapshotError("maintenance timestamps are invalid")
    if not isinstance(last["rollback_performed"], bool):
        raise SnapshotError("maintenance rollback state is invalid")
    if not _is_nullable_text(last["failure_reason"], limit=200):
        raise SnapshotError("maintenance failure reason is invalid")


def _validate_modpack(value: object) -> None:
    modpack = _require_mapping(value, "modpack")
    _require_keys(modpack, "modpack", {"active_release", "previous_release", "packages"})
    if not _is_nullable_text(modpack["active_release"], limit=128) or not _is_nullable_text(
        modpack["previous_release"], limit=128
    ):
        raise SnapshotError("modpack release is invalid")
    packages = modpack["packages"]
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise SnapshotError("modpack packages are invalid")
    for package in packages:
        entry = _require_mapping(package, "modpack package")
        _require_keys(entry, "modpack package", {"namespace", "name", "version", "role"})
        if not all(
            isinstance(entry[field], str)
            and PACKAGE_IDENTIFIER.fullmatch(entry[field]) is not None
            for field in ("namespace", "name", "version")
        ):
            raise SnapshotError("modpack package identifier is invalid")
        if entry["role"] not in {"server", "client"}:
            raise SnapshotError("modpack package role is invalid")


def _validate_logs(value: object) -> None:
    logs = _require_mapping(value, "logs")
    _require_keys(logs, "logs", {"server", "maintenance"})
    for name in ("server", "maintenance"):
        entries = logs[name]
        if not isinstance(entries, list) or len(entries) > MAX_LOG_ENTRIES:
            raise SnapshotError(f"{name} logs are invalid")
        if not all(_is_text(entry, limit=MAX_LOG_ENTRY_CHARACTERS) for entry in entries):
            raise SnapshotError(f"{name} log entry is invalid")


def validate_snapshot(value: object) -> dict[str, Any]:
    """Validate the complete version-one snapshot before exposing it to the UI."""

    snapshot = _require_mapping(value, "snapshot")
    _require_keys(
        snapshot,
        "snapshot",
        {"schema_version", "generated_at", "server", "maintenance", "modpack", "logs"},
    )
    if snapshot["schema_version"] != 1:
        raise SnapshotError("snapshot schema version is unsupported")
    if not _is_timestamp(snapshot["generated_at"]):
        raise SnapshotError("snapshot timestamp is invalid")
    _validate_server(snapshot["server"])
    _validate_maintenance(snapshot["maintenance"])
    _validate_modpack(snapshot["modpack"])
    _validate_logs(snapshot["logs"])
    return snapshot


def load_snapshot(path: Path) -> dict[str, Any]:
    """Load one bounded snapshot, distinguishing absence from invalid content."""

    try:
        with path.open("rb") as snapshot_file:
            if not stat.S_ISREG(os.fstat(snapshot_file.fileno()).st_mode):
                raise SnapshotError("status snapshot is not a regular file")
            if os.fstat(snapshot_file.fileno()).st_size > MAX_SNAPSHOT_BYTES:
                raise SnapshotError("snapshot exceeds the maximum size")
            contents = snapshot_file.read(MAX_SNAPSHOT_BYTES + 1)
    except FileNotFoundError as error:
        raise SnapshotUnavailable("status snapshot is not available yet") from error
    except OSError as error:
        raise SnapshotUnavailable("status snapshot cannot be read") from error
    if len(contents) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds the maximum size")
    try:
        return validate_snapshot(json.loads(contents.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SnapshotError("status snapshot is not valid JSON") from error
