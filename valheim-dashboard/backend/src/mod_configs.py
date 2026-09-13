"""Validation for the bounded root-exported BepInEx configuration snapshot."""

import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

MAX_EXPORT_BYTES = 256 * 1024
MAX_FILES = 50
MAX_ENTRIES = 500
MAX_PATH_PARTS = 4
MAX_PATH_PART_CHARACTERS = 64
MAX_SECTION_CHARACTERS = 128
MAX_KEY_CHARACTERS = 128
MAX_VALUE_CHARACTERS = 2 * 1024
_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class ConfigSnapshotError(ValueError):
    """The root-exported configuration snapshot is unavailable or invalid."""

    code = "mod_configs_unavailable"


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigSnapshotError(f"{name} is invalid")
    return value


def _validate_path(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigSnapshotError("configuration path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or len(path.parts) > MAX_PATH_PARTS
        or any(
            len(part) > MAX_PATH_PART_CHARACTERS or _PATH_PART.fullmatch(part) is None
            for part in path.parts
        )
        or not path.name.endswith(".cfg")
    ):
        raise ConfigSnapshotError("configuration path is invalid")
    return path.as_posix()


def _validate_entry(value: object) -> dict[str, str]:
    entry = _require_mapping(value, "configuration entry")
    if set(entry) != {"section", "key", "value"}:
        raise ConfigSnapshotError("configuration entry is invalid")
    section = entry["section"]
    key = entry["key"]
    config_value = entry["value"]
    if (
        not isinstance(section, str)
        or not section
        or len(section) > MAX_SECTION_CHARACTERS
        or "\r" in section
        or "\n" in section
        or not isinstance(key, str)
        or not key
        or len(key) > MAX_KEY_CHARACTERS
        or "\r" in key
        or "\n" in key
        or not isinstance(config_value, str)
        or len(config_value) > MAX_VALUE_CHARACTERS
        or "\r" in config_value
        or "\n" in config_value
    ):
        raise ConfigSnapshotError("configuration entry is invalid")
    return {"section": section, "key": key, "value": config_value}


def validate_config_export(value: object) -> dict[str, object]:
    document = _require_mapping(value, "configuration export")
    if set(document) != {"schema_version", "files", "errors"} or document["schema_version"] != 1:
        raise ConfigSnapshotError("configuration export schema is invalid")
    files = document["files"]
    errors = document["errors"]
    if not isinstance(files, list) or len(files) > MAX_FILES or not isinstance(errors, list):
        raise ConfigSnapshotError("configuration export is invalid")

    normalized_files: list[dict[str, object]] = []
    paths: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    entry_count = 0
    for item in files:
        file = _require_mapping(item, "configuration file")
        if set(file) != {"path", "entries"} or not isinstance(file["entries"], list):
            raise ConfigSnapshotError("configuration file is invalid")
        path = _validate_path(file["path"])
        if path in paths:
            raise ConfigSnapshotError("configuration export has duplicate files")
        paths.add(path)
        entries = []
        for value in file["entries"]:
            entry = _validate_entry(value)
            identity = (path, entry["section"], entry["key"])
            if identity in identities:
                raise ConfigSnapshotError("configuration export has duplicate entries")
            identities.add(identity)
            entries.append(entry)
            entry_count += 1
            if entry_count > MAX_ENTRIES:
                raise ConfigSnapshotError("configuration export exceeds the entry limit")
        normalized_files.append({"path": path, "entries": entries})

    normalized_errors: list[dict[str, str]] = []
    if len(errors) > MAX_FILES:
        raise ConfigSnapshotError("configuration export has too many errors")
    for value in errors:
        error = _require_mapping(value, "configuration export error")
        if set(error) != {"path", "message"}:
            raise ConfigSnapshotError("configuration export error is invalid")
        path = _validate_path(error["path"])
        message = error["message"]
        if not isinstance(message, str) or not message or len(message) > 256:
            raise ConfigSnapshotError("configuration export error is invalid")
        normalized_errors.append({"path": path, "message": message})
    return {"schema_version": 1, "files": normalized_files, "errors": normalized_errors}


def load_config_export(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as config_file:
            if not stat.S_ISREG(path.stat().st_mode):
                raise ConfigSnapshotError("configuration export is invalid")
            contents = config_file.read(MAX_EXPORT_BYTES + 1)
    except FileNotFoundError as error:
        raise ConfigSnapshotError("configuration export is not available yet") from error
    except OSError as error:
        raise ConfigSnapshotError("configuration export could not be read") from error
    if len(contents) > MAX_EXPORT_BYTES:
        raise ConfigSnapshotError("configuration export exceeds the size limit")
    try:
        return validate_config_export(json.loads(contents.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ConfigSnapshotError("configuration export is invalid") from error


def validate_request_targets(
    request: dict[str, object], config_export: dict[str, object]
) -> None:
    available = {
        (file["path"], entry["section"], entry["key"])
        for file in config_export["files"]
        for entry in file["entries"]
    }
    for file in request["files"]:
        for update in file["updates"]:
            if (file["path"], update["section"], update["key"]) not in available:
                raise ConfigSnapshotError("configuration request targets an unknown file or key")
