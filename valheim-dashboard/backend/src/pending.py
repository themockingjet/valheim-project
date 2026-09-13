"""Fixed-schema pending job transport for privileged directory-job helpers.

The dashboard backend never touches modpack, server, or systemd state
directly. Instead it validates an operator request against a strict fixed
schema and atomically writes an opaque JSON job file into a dashboard-owned
pending directory. A separate root-owned systemd path/service helper is the
only thing that reads that directory, re-validates independently, performs
the bounded privileged action, and writes a bounded result file back into a
state directory this backend can read (but not write to, other than the
pending directory itself).
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

MANIFEST_REQUEST_FILE = "manifest-request.json"
CONFIG_REQUEST_FILE = "config-request.json"
UPDATE_REQUEST_FILE = "update-request.json"
ROLLBACK_REQUEST_FILE = "rollback-request.json"
WORLD_RESTORE_REQUEST_FILE = "world-restore-request.json"

MANIFEST_RESULT_FILE = "manifest-result.json"
CONFIG_RESULT_FILE = "config-result.json"
UPDATE_RESULT_FILE = "update-result.json"
ROLLBACK_RESULT_FILE = "rollback-result.json"
WORLD_RESTORE_RESULT_FILE = "world-restore-result.json"

MAX_PACKAGES = 50
MAX_CONFIG_FILES = 50
MAX_CONFIG_UPDATES = 500
MAX_CONFIG_VALUE_CHARACTERS = 2 * 1024
MAX_CONFIG_REQUEST_BYTES = 48 * 1024
MAX_REASON_CHARACTERS = 200
MAX_RESULT_BYTES = 64 * 1024

NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}\Z")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
VERSION_PATTERN = re.compile(r"^(latest|[A-Za-z0-9][A-Za-z0-9.+-]{0,63})\Z")
WORLD_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,64}\Z")
CONFIG_PATH_PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class ValidationError(ValueError):
    """A pending request failed strict fixed-schema validation."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: object, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(body)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def validate_manifest_packages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValidationError("packages must be a non-empty list")
    if len(value) > MAX_PACKAGES:
        raise ValidationError(f"packages must not exceed {MAX_PACKAGES} entries")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        entry = _require_mapping(item, f"packages[{index}]")
        if set(entry) - {"namespace", "name", "version", "channel", "role"}:
            raise ValidationError(f"packages[{index}] has an unsupported field")

        namespace = entry.get("namespace")
        name = entry.get("name")
        version = entry.get("version")
        channel = entry.get("channel", "stable")
        role = entry.get("role", "server")

        if not isinstance(namespace, str) or not NAMESPACE_PATTERN.fullmatch(namespace):
            raise ValidationError(f"packages[{index}].namespace is invalid")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise ValidationError(f"packages[{index}].name is invalid")
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            raise ValidationError(f"packages[{index}].version is invalid")
        if channel not in {"stable", "prerelease"}:
            raise ValidationError(f"packages[{index}].channel must be stable or prerelease")
        if role not in {"server", "client"}:
            raise ValidationError(f"packages[{index}].role must be server or client")

        key = (namespace, name)
        if key in seen:
            raise ValidationError(f"packages[{index}] duplicates an earlier entry")
        seen.add(key)
        normalized.append(
            {
                "source": "hexium",
                "namespace": namespace,
                "name": name,
                "version": version,
                "channel": channel,
                "role": role,
            }
        )
    return normalized


def build_manifest_request(payload: object) -> dict[str, Any]:
    body = _require_mapping(payload, "request body")
    if set(body) - {"packages"}:
        raise ValidationError("request body has an unsupported field")
    packages = validate_manifest_packages(body.get("packages"))
    return {
        "schema_version": 1,
        "requested_at": _now_iso(),
        "packages": packages,
    }


def _validate_config_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("configuration path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or len(path.parts) > 4
        or any(CONFIG_PATH_PART_PATTERN.fullmatch(part) is None for part in path.parts)
        or not path.name.endswith(".cfg")
    ):
        raise ValidationError("configuration path is invalid")
    return path.as_posix()


def build_config_request(payload: object) -> dict[str, Any]:
    body = _require_mapping(payload, "request body")
    if set(body) != {"files"}:
        raise ValidationError("request body has an unsupported field")
    files = body["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_CONFIG_FILES:
        raise ValidationError("files must be a non-empty bounded list")

    normalized_files: list[dict[str, object]] = []
    paths: set[str] = set()
    update_count = 0
    for file_index, file in enumerate(files):
        entry = _require_mapping(file, f"files[{file_index}]")
        if set(entry) != {"path", "updates"}:
            raise ValidationError(f"files[{file_index}] has an unsupported field")
        path = _validate_config_path(entry["path"])
        if path in paths:
            raise ValidationError(f"files[{file_index}] duplicates an earlier file")
        paths.add(path)
        updates = entry["updates"]
        if not isinstance(updates, list) or not updates:
            raise ValidationError(f"files[{file_index}].updates must be a non-empty list")
        normalized_updates: list[dict[str, str]] = []
        identities: set[tuple[str, str]] = set()
        for update_index, update in enumerate(updates):
            item = _require_mapping(update, f"files[{file_index}].updates[{update_index}]")
            if set(item) != {"section", "key", "value"}:
                raise ValidationError(
                    f"files[{file_index}].updates[{update_index}] has an unsupported field"
                )
            section = item["section"]
            key = item["key"]
            value = item["value"]
            if (
                not isinstance(section, str)
                or not section
                or len(section) > 128
                or "\r" in section
                or "\n" in section
                or not isinstance(key, str)
                or not key
                or len(key) > 128
                or "\r" in key
                or "\n" in key
                or not isinstance(value, str)
                or len(value) > MAX_CONFIG_VALUE_CHARACTERS
                or "\r" in value
                or "\n" in value
            ):
                raise ValidationError(f"files[{file_index}].updates[{update_index}] is invalid")
            identity = (section, key)
            if identity in identities:
                raise ValidationError(f"files[{file_index}].updates duplicates an earlier key")
            identities.add(identity)
            normalized_updates.append({"section": section, "key": key, "value": value})
            update_count += 1
            if update_count > MAX_CONFIG_UPDATES:
                raise ValidationError(f"files must not exceed {MAX_CONFIG_UPDATES} updates")
        normalized_files.append({"path": path, "updates": normalized_updates})

    request = {
        "schema_version": 1,
        "requested_at": _now_iso(),
        "files": normalized_files,
    }
    if len(json.dumps(request, separators=(",", ":")).encode("utf-8")) > MAX_CONFIG_REQUEST_BYTES:
        raise ValidationError("configuration request body is too large")
    return request


def _validate_reason(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_REASON_CHARACTERS:
        raise ValidationError("reason must be a short string")
    return value


def build_update_request(payload: object) -> dict[str, Any]:
    body = _require_mapping(payload, "request body")
    if set(body) - {"confirmation", "reason"}:
        raise ValidationError("request body has an unsupported field")
    if body.get("confirmation") != "UPDATE":
        raise ValidationError('confirmation must be exactly "UPDATE"')
    return {
        "schema_version": 1,
        "requested_at": _now_iso(),
        "reason": _validate_reason(body.get("reason")),
    }


def build_rollback_request(payload: object) -> dict[str, Any]:
    body = _require_mapping(payload, "request body")
    if set(body) - {"confirmation", "reason"}:
        raise ValidationError("request body has an unsupported field")
    if body.get("confirmation") != "ROLLBACK":
        raise ValidationError('confirmation must be exactly "ROLLBACK"')
    return {
        "schema_version": 1,
        "requested_at": _now_iso(),
        "reason": _validate_reason(body.get("reason")),
    }


def build_world_restore_request(payload: object) -> dict[str, Any]:
    """Normalize a confirmed restore request without accepting a filesystem path."""

    body = _require_mapping(payload, "request body")
    if set(body) - {"backup_id", "confirmation", "reason"}:
        raise ValidationError("request body has an unsupported field")
    backup_id = body.get("backup_id")
    if not isinstance(backup_id, str) or WORLD_BACKUP_ID_PATTERN.fullmatch(backup_id) is None:
        raise ValidationError("backup_id is invalid")
    if body.get("confirmation") != "RESTORE":
        raise ValidationError('confirmation must be exactly "RESTORE"')
    return {
        "schema_version": 1,
        "requested_at": _now_iso(),
        "backup_id": backup_id,
        "reason": _validate_reason(body.get("reason")),
    }


def write_pending_request(pending_directory: Path, file_name: str, request: dict[str, Any]) -> None:
    """Atomically create the pending job file, refusing to overwrite one in flight."""

    target = pending_directory / file_name
    if target.exists():
        raise FileExistsError(file_name)
    _atomic_write_json(target, request, mode=0o600)


def read_pending_request(pending_directory: Path, file_name: str) -> dict[str, Any] | None:
    path = pending_directory / file_name
    try:
        with path.open("rb") as pending_file:
            contents = pending_file.read(MAX_RESULT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if len(contents) > MAX_RESULT_BYTES:
        return None
    try:
        value = json.loads(contents.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_result(state_directory: Path, file_name: str) -> dict[str, Any] | None:
    """Read a bounded, root-written result file; treat anything unexpected as absent."""

    path = state_directory / file_name
    try:
        with path.open("rb") as result_file:
            contents = result_file.read(MAX_RESULT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if len(contents) > MAX_RESULT_BYTES:
        return None
    try:
        value = json.loads(contents.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None
