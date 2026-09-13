"""Validated release staging, atomic activation, and rollback."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any
from zipfile import BadZipFile, ZipFile

from .errors import ModpackError
from .hexium import HexiumClient

BASE_PACKAGE = ("denikson", "BepInExPack_Valheim")
MAX_ARCHIVE_ENTRIES = 10_000
MAX_UNCOMPRESSED_ARCHIVE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class LockedPackage:
    """A validated server package from a generated lock file."""

    namespace: str
    name: str
    version: str
    full_name: str
    download_url: str
    sha256: str
    role: str

    @property
    def key(self) -> tuple[str, str]:
        return self.namespace, self.name


def _atomic_bytes(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.replace(temp_path, path)


def _atomic_symlink(path: Path, target: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    temporary.symlink_to(target)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    with path.open("rb") as archive_file:
        return hashlib.file_digest(archive_file, "sha256").hexdigest()


def load_lock(path: Path) -> list[LockedPackage]:
    """Load a generated lock and reject paths, URLs, or checksums it cannot trust."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ModpackError(f"Cannot read lock {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ModpackError(f"Cannot parse lock {path}: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModpackError("lock schema_version must be 1")
    if raw.get("source") != "hexium":
        raise ModpackError("lock source must be 'hexium'")
    entries = raw.get("packages")
    if not isinstance(entries, list) or not entries:
        raise ModpackError("lock packages must be a non-empty list")

    packages: list[LockedPackage] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ModpackError(f"lock packages[{index}] must be a mapping")
        fields = ("namespace", "name", "version", "full_name", "download_url", "sha256", "role")
        if not all(isinstance(entry.get(field), str) and entry[field] for field in fields):
            raise ModpackError(f"lock packages[{index}] has missing or invalid fields")
        package = LockedPackage(*(entry[field] for field in fields))
        if package.key in seen:
            raise ModpackError(f"Duplicate lock package: {package.namespace}-{package.name}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", package.full_name):
            raise ModpackError(f"Unsafe package full_name: {package.full_name!r}")
        if not package.download_url.startswith("https://cdn.hexium.gg/"):
            raise ModpackError(f"Package download is not a Hexium CDN URL: {package.download_url!r}")
        if not re.fullmatch(r"[a-f0-9]{64}", package.sha256):
            raise ModpackError(f"Invalid SHA-256 for {package.full_name}")
        if package.role not in {"server", "client"}:
            raise ModpackError(f"Invalid role for {package.full_name}: {package.role!r}")
        seen.add(package.key)
        packages.append(package)
    return packages


def ensure_archive(
    package: LockedPackage, cache_directory: Path, client: HexiumClient
) -> Path:
    """Download or re-verify a locked archive, rejecting a tampered cache entry."""

    cache_path = cache_directory / f"{package.full_name}.zip"
    if cache_path.exists() and _sha256(cache_path) == package.sha256:
        return cache_path

    _atomic_bytes(cache_path, client.download(package.download_url))
    if _sha256(cache_path) != package.sha256:
        cache_path.unlink(missing_ok=True)
        raise ModpackError(f"SHA-256 mismatch for {package.full_name}")
    return cache_path


def _validate_member(name: str, mode: int) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ModpackError(f"Unsafe archive member path: {name!r}")
    if stat.S_ISLNK(mode):
        raise ModpackError(f"Archive contains unsupported symlink: {name!r}")
    return path


def _extract_archive(archive_path: Path, destination: Path) -> None:
    """Extract a bounded ZIP archive after rejecting traversal and symlink entries."""

    try:
        with ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ModpackError(f"Archive has too many entries: {archive_path.name}")
            if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_ARCHIVE_BYTES:
                raise ModpackError(f"Archive is too large when extracted: {archive_path.name}")
            checked_entries = [
                (entry, _validate_member(entry.filename, entry.external_attr >> 16))
                for entry in entries
            ]
            for entry, relative_path in checked_entries:
                target = destination.joinpath(*relative_path.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(target, 0o755 if entry.external_attr >> 16 & 0o111 else 0o644)
    except BadZipFile as error:
        raise ModpackError(f"Package is not a valid ZIP archive: {archive_path.name}") from error


def _payload_root(extraction: Path) -> Path:
    """Select the one package root while ignoring package metadata files."""

    if (extraction / "BepInEx").is_dir() or (extraction / "plugins").is_dir():
        return extraction
    directories = [path for path in extraction.iterdir() if path.is_dir()]
    if len(directories) == 1:
        return directories[0]
    raise ModpackError(f"Cannot identify package payload root in {extraction.name}")


def _copy_contents(source: Path, destination: Path) -> None:
    for item in source.iterdir():
        target = destination / item.name
        if item.is_symlink():
            raise ModpackError(f"Extracted package contains unsupported symlink: {item}")
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _stage_base_package(package: LockedPackage, archive: Path, release: Path) -> None:
    with tempfile.TemporaryDirectory(dir=release, prefix=".extract-base-") as temporary:
        extraction = Path(temporary)
        _extract_archive(archive, extraction)
        payload = _payload_root(extraction)
        _copy_contents(payload, release)
    required_paths = (
        release / "BepInEx" / "core" / "BepInEx.Preloader.dll",
        release / "doorstop_libs" / "libdoorstop_x64.so",
    )
    if not all(required_path.is_file() for required_path in required_paths):
        raise ModpackError("BepInEx package is missing required Linux loader files")


def _stage_mod_package(package: LockedPackage, archive: Path, release: Path) -> None:
    with tempfile.TemporaryDirectory(dir=release, prefix=".extract-mod-") as temporary:
        extraction = Path(temporary)
        _extract_archive(archive, extraction)
        payload = _payload_root(extraction)
        if (payload / "BepInEx").is_dir():
            _copy_contents(payload / "BepInEx", release / "BepInEx")
            return
        forbidden = ("doorstop_libs", "doorstop_config.ini", ".doorstop_version", "winhttp.dll")
        if any((payload / item).exists() for item in forbidden):
            raise ModpackError(f"Non-base package may not provide loader files: {package.full_name}")
        destination = release / "BepInEx" / "plugins" / package.full_name
        destination.mkdir(parents=True, exist_ok=True)
        _copy_contents(payload, destination)


def _copy_config_overrides(config_overrides: Path, release: Path) -> None:
    if not config_overrides.exists():
        return
    if not config_overrides.is_dir() or config_overrides.is_symlink():
        raise ModpackError("config-overrides must be a real directory")
    for source in config_overrides.rglob("*"):
        relative = source.relative_to(config_overrides)
        if source.is_symlink():
            raise ModpackError(f"config-overrides contains unsupported symlink: {relative}")
        if source.is_dir():
            continue
        target = release / "BepInEx" / "config" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def stage_release(
    packages: list[LockedPackage],
    *,
    releases_directory: Path,
    cache_directory: Path,
    config_overrides: Path,
    client: HexiumClient,
) -> Path:
    """Build a complete release without touching the active deployment."""

    server_packages = [package for package in packages if package.role == "server"]
    base_matches = [package for package in server_packages if package.key == BASE_PACKAGE]
    if len(base_matches) != 1:
        raise ModpackError("A server deployment requires exactly one denikson-BepInExPack_Valheim")

    releases_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=releases_directory, prefix=".staging-"
    ) as temporary:
        staging = Path(temporary)
        base_package = base_matches[0]
        _stage_base_package(
            base_package, ensure_archive(base_package, cache_directory, client), staging
        )
        for package in server_packages:
            if package == base_package:
                continue
            _stage_mod_package(
                package, ensure_archive(package, cache_directory, client), staging
            )
        _copy_config_overrides(config_overrides, staging)
        _atomic_bytes(
            staging / "release-lock.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "packages": [package.full_name for package in server_packages],
                },
                indent=2,
                sort_keys=True,
            ).encode()
            + b"\n",
        )
        release_name = f"release-{os.urandom(8).hex()}"
        release = releases_directory / release_name
        os.replace(staging, release)
    return release


def _release_target(releases_directory: Path, link: Path) -> str | None:
    if not link.is_symlink():
        return None
    resolved = link.resolve(strict=True)
    releases_root = releases_directory.resolve(strict=True)
    try:
        resolved.relative_to(releases_root)
    except ValueError as error:
        raise ModpackError(f"{link.name} points outside the releases directory") from error
    if not (resolved / "release-lock.json").is_file():
        raise ModpackError(f"{link.name} does not point to a valid release")
    return str(resolved)


def activate_release(root: Path, release: Path) -> None:
    """Atomically switch the active release while retaining one rollback target."""

    releases_directory = root / "releases"
    release = release.resolve(strict=True)
    try:
        release.relative_to(releases_directory.resolve(strict=True))
    except ValueError as error:
        raise ModpackError("Refusing to activate a release outside the release directory") from error
    if not (release / "release-lock.json").is_file():
        raise ModpackError("Refusing to activate an incomplete release")

    current = root / "current"
    previous = root / "previous"
    previous_target = _release_target(releases_directory, current)
    if previous_target is not None:
        _atomic_symlink(previous, previous_target)
    _atomic_symlink(current, str(release))


def rollback(root: Path) -> Path:
    """Atomically restore the previous known-good release."""

    releases_directory = root / "releases"
    current = root / "current"
    previous = root / "previous"
    previous_target = _release_target(releases_directory, previous)
    if previous_target is None:
        raise ModpackError("No previous release is available for rollback")
    current_target = _release_target(releases_directory, current)
    _atomic_symlink(current, previous_target)
    if current_target is not None:
        _atomic_symlink(previous, current_target)
    return Path(previous_target)
