"""Bounded BepInEx configuration export and queued override application."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any

from .errors import ModpackError

MAX_CONFIG_FILES = 50
MAX_CONFIG_FILE_BYTES = 32 * 1024
MAX_CONFIG_EXPORT_BYTES = 256 * 1024
MAX_PATH_PARTS = 4
MAX_PATH_PART_CHARACTERS = 64
MAX_SECTION_CHARACTERS = 128
MAX_KEY_CHARACTERS = 128
MAX_VALUE_CHARACTERS = 2 * 1024
MAX_UPDATES = 500
MAX_REQUEST_BYTES = 48 * 1024

_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SECTION = re.compile(r"^\[(?P<section>[^\[\]\r\n]{1,128})\]\s*(?:\r?\n)?\Z")
_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*)(?P<key>[^=;\s#\r\n][^=\r\n]*?)"
    r"(?P<separator>\s*=\s*)(?P<value>[^\r\n]*)(?P<ending>\r?\n)?\Z"
)


class ConfigError(ModpackError):
    """A managed BepInEx configuration document cannot be safely processed."""


@dataclass(frozen=True)
class ConfigEntry:
    section: str
    key: str
    value: str
    line_index: int
    value_prefix: str
    line_ending: str

    def to_dict(self) -> dict[str, str]:
        return {"section": self.section, "key": self.key, "value": self.value}


def _relative_config_path(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) == 0 or len(relative.parts) > MAX_PATH_PARTS:
        return None
    if any(_PATH_PART.fullmatch(part) is None for part in relative.parts):
        return None
    if not relative.name.endswith(".cfg"):
        return None
    return PurePosixPath(*relative.parts).as_posix()


def _iter_config_files(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    if not root.is_dir() or root.is_symlink():
        raise ConfigError(f"configuration root is invalid: {root}")

    files: dict[str, Path] = {}
    for path in root.rglob("*.cfg"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = _relative_config_path(path, root)
        if relative is not None:
            files[relative] = path
    return files


def _active_config_directory(modpack_root: Path) -> Path | None:
    current = modpack_root / "current"
    if not current.is_symlink():
        return None
    try:
        release = current.resolve(strict=True)
        releases = (modpack_root / "releases").resolve(strict=True)
        release.relative_to(releases)
    except (OSError, RuntimeError, ValueError):
        return None
    if not (release / "release-lock.json").is_file():
        return None
    config_directory = release / "BepInEx" / "config"
    return config_directory if config_directory.is_dir() and not config_directory.is_symlink() else None


def _read_config(path: Path) -> str:
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise ConfigError(f"cannot read configuration {path.name}") from error
    if len(contents) > MAX_CONFIG_FILE_BYTES:
        raise ConfigError(f"configuration {path.name} exceeds the size limit")
    try:
        return contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigError(f"configuration {path.name} is not UTF-8") from error


def parse_config(contents: str) -> list[ConfigEntry]:
    """Parse editable BepInEx assignments while retaining original line details."""

    entries: list[ConfigEntry] = []
    section = ""
    seen: set[tuple[str, str]] = set()
    for line_index, line in enumerate(contents.splitlines(keepends=True)):
        section_match = _SECTION.fullmatch(line)
        if section_match is not None:
            section = section_match.group("section").strip()
            continue
        assignment = _ASSIGNMENT.fullmatch(line)
        if assignment is None:
            continue
        key = assignment.group("key").strip()
        value = assignment.group("value")
        if (
            not section
            or not key
            or len(key) > MAX_KEY_CHARACTERS
            or len(value) > MAX_VALUE_CHARACTERS
        ):
            continue
        identity = (section, key)
        if identity in seen:
            raise ConfigError(f"configuration has duplicate key [{section}] {key}")
        seen.add(identity)
        entries.append(
            ConfigEntry(
                section=section,
                key=key,
                value=value,
                line_index=line_index,
                value_prefix=assignment.group("prefix")
                + assignment.group("key")
                + assignment.group("separator"),
                line_ending=assignment.group("ending") or "",
            )
        )
        if len(entries) > MAX_UPDATES:
            raise ConfigError("configuration exceeds the entry limit")
    return entries


def _configuration_sources(modpack_root: Path) -> dict[str, Path]:
    release_config = _active_config_directory(modpack_root)
    sources = _iter_config_files(release_config) if release_config is not None else {}
    sources.update(_iter_config_files(modpack_root / "config-overrides"))
    if len(sources) > MAX_CONFIG_FILES:
        raise ConfigError("managed configuration exceeds the file limit")
    return dict(sorted(sources.items()))


def build_config_export(modpack_root: Path) -> dict[str, object]:
    """Build the separately exported, bounded dashboard configuration view."""

    files = []
    errors = []
    for relative_path, source in _configuration_sources(modpack_root).items():
        try:
            entries = parse_config(_read_config(source))
        except ConfigError as error:
            errors.append({"path": relative_path, "message": str(error)})
            continue
        files.append(
            {
                "path": relative_path,
                "entries": [entry.to_dict() for entry in entries],
            }
        )
    document: dict[str, object] = {"schema_version": 1, "files": files, "errors": errors}
    encoded = (json.dumps(document, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(encoded) > MAX_CONFIG_EXPORT_BYTES:
        raise ConfigError("configuration export exceeds the size limit")
    return document


def _atomic_write(path: Path, contents: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o640)
    os.replace(temporary_path, path)


def _override_destination(override_root: Path, relative_path: str) -> Path:
    if override_root.exists() and (not override_root.is_dir() or override_root.is_symlink()):
        raise ConfigError("config-overrides must be a real directory")
    override_root.mkdir(parents=True, exist_ok=True)
    parent = override_root
    for part in PurePosixPath(relative_path).parts[:-1]:
        parent = parent / part
        if parent.exists():
            if not parent.is_dir() or parent.is_symlink():
                raise ConfigError("configuration override parent is invalid")
        else:
            parent.mkdir()
    destination = parent / PurePosixPath(relative_path).name
    if destination.is_symlink():
        raise ConfigError("configuration override target is invalid")
    return destination


def publish_config_export(
    modpack_root: Path, output: Path, *, group_id: int | None = None
) -> None:
    document = build_config_export(modpack_root)
    contents = (json.dumps(document, separators=(",", ":"), ensure_ascii=True) + "\n")
    _atomic_write(output, contents)
    os.chmod(output, 0o640)
    if group_id is not None:
        os.chown(output, 0, group_id)


def _validated_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError("configuration path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) == 0
        or len(path.parts) > MAX_PATH_PARTS
        or any(_PATH_PART.fullmatch(part) is None for part in path.parts)
        or not path.name.endswith(".cfg")
    ):
        raise ConfigError("configuration path is invalid")
    return path.as_posix()


def validate_config_request(request: object) -> list[dict[str, object]]:
    if not isinstance(request, dict) or set(request) != {
        "schema_version",
        "requested_at",
        "files",
    }:
        raise ConfigError("pending configuration request has an unsupported shape")
    if request["schema_version"] != 1 or not isinstance(request["requested_at"], str):
        raise ConfigError("pending configuration request is invalid")
    if len(request["requested_at"]) > 40:
        raise ConfigError("pending configuration request timestamp is invalid")
    files = request["files"]
    if not isinstance(files, list) or not files:
        raise ConfigError("pending configuration request files are invalid")

    normalized: list[dict[str, object]] = []
    paths: set[str] = set()
    update_count = 0
    for file_index, file in enumerate(files):
        if not isinstance(file, dict) or set(file) != {"path", "updates"}:
            raise ConfigError(f"pending configuration request files[{file_index}] is invalid")
        path = _validated_relative_path(file["path"])
        if path in paths:
            raise ConfigError("pending configuration request duplicates a file")
        paths.add(path)
        updates = file["updates"]
        if not isinstance(updates, list) or not updates:
            raise ConfigError(f"pending configuration request files[{file_index}].updates is invalid")
        normalized_updates = []
        identities: set[tuple[str, str]] = set()
        for update_index, update in enumerate(updates):
            if not isinstance(update, dict) or set(update) != {"section", "key", "value"}:
                raise ConfigError(
                    f"pending configuration request files[{file_index}].updates[{update_index}] is invalid"
                )
            section = update["section"]
            key = update["key"]
            value = update["value"]
            if (
                not isinstance(section, str)
                or not section
                or len(section) > MAX_SECTION_CHARACTERS
                or "\n" in section
                or "\r" in section
                or not isinstance(key, str)
                or not key
                or len(key) > MAX_KEY_CHARACTERS
                or "\n" in key
                or "\r" in key
                or not isinstance(value, str)
                or len(value) > MAX_VALUE_CHARACTERS
                or "\n" in value
                or "\r" in value
            ):
                raise ConfigError(
                    f"pending configuration request files[{file_index}].updates[{update_index}] is invalid"
                )
            identity = (section, key)
            if identity in identities:
                raise ConfigError("pending configuration request duplicates a key")
            identities.add(identity)
            normalized_updates.append({"section": section, "key": key, "value": value})
            update_count += 1
            if update_count > MAX_UPDATES:
                raise ConfigError("pending configuration request exceeds the update limit")
        normalized.append({"path": path, "updates": normalized_updates})

    encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ConfigError("pending configuration request exceeds the size limit")
    return normalized


def apply_config_request(request: object, *, modpack_root: Path) -> list[dict[str, object]]:
    """Merge validated value updates into the fixed persistent override directory."""

    updates_by_file = {item["path"]: item["updates"] for item in validate_config_request(request)}
    sources = _configuration_sources(modpack_root)
    results: list[dict[str, object]] = []
    prepared: list[tuple[Path, str]] = []
    override_root = modpack_root / "config-overrides"

    for relative_path, updates in updates_by_file.items():
        source = sources.get(relative_path)
        if source is None:
            raise ConfigError(f"configuration file is not managed: {relative_path}")
        contents = _read_config(source)
        entries = parse_config(contents)
        by_identity = {(entry.section, entry.key): entry for entry in entries}
        for update in updates:
            identity = (update["section"], update["key"])
            if identity not in by_identity:
                raise ConfigError(
                    f"configuration key is not managed: [{identity[0]}] {identity[1]}"
                )

        lines = contents.splitlines(keepends=True)
        for update in updates:
            entry = by_identity[(update["section"], update["key"])]
            lines[entry.line_index] = entry.value_prefix + update["value"] + entry.line_ending
        destination = _override_destination(override_root, relative_path)
        prepared.append((destination, "".join(lines)))
        results.append({"path": relative_path, "updated_keys": len(updates)})
    for destination, contents in prepared:
        _atomic_write(destination, contents)
    return results
