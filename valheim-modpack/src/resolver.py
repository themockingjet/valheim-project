"""Hexium dependency resolution and reproducible lock-file generation."""

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
from zipfile import BadZipFile, ZipFile

from .errors import ModpackError
from .manifest import ManifestPackage
from .versioning import at_least, is_prerelease

DEPENDENCY_PATTERN = re.compile(
    r"^(?P<namespace>[A-Za-z0-9_]+)-(?P<name>[A-Za-z0-9_-]+)-"
    r"(?P<minimum>\d+(?:\.\d+)*(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)$"
)


class PackageClient(Protocol):
    """Required Hexium operations, allowing deterministic tests."""

    def get_package(self, namespace: str, name: str) -> dict[str, Any]: ...

    def get_version(self, namespace: str, name: str, version: str) -> dict[str, Any]: ...

    def download(self, url: str) -> bytes: ...


@dataclass
class Requirement:
    """Collected direct/dependency constraints for one package."""

    namespace: str
    name: str
    exact_versions: set[str] = field(default_factory=set)
    minimum_versions: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    allow_prerelease: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.namespace, self.name

    @property
    def role(self) -> str:
        return "server" if "server" in self.roles else "client"


def _package_identity(metadata: dict[str, Any], expected: tuple[str, str]) -> tuple[str, str]:
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if (namespace, name) != expected:
        raise ModpackError(
            f"Hexium metadata identity mismatch: expected {expected[0]}-{expected[1]}"
        )
    return namespace, name


def _metadata_version(metadata: dict[str, Any], expected: tuple[str, str], version: str) -> None:
    _package_identity(metadata, expected)
    if metadata.get("version_number") != version:
        raise ModpackError(
            f"Hexium returned {metadata.get('version_number')!r} for "
            f"{expected[0]}-{expected[1]}-{version}"
        )
    if metadata.get("is_active") is not True:
        raise ModpackError(f"Package version is inactive: {expected[0]}-{expected[1]}-{version}")
    if not isinstance(metadata.get("download_url"), str):
        raise ModpackError(f"Package has no download URL: {expected[0]}-{expected[1]}-{version}")
    if not isinstance(metadata.get("dependencies"), list) or not all(
        isinstance(item, str) for item in metadata["dependencies"]
    ):
        raise ModpackError(
            f"Package has invalid dependencies: {expected[0]}-{expected[1]}-{version}"
        )


def _parse_dependency(value: str) -> tuple[str, str, str]:
    match = DEPENDENCY_PATTERN.fullmatch(value)
    if match is None:
        raise ModpackError(f"Unsupported Hexium dependency declaration: {value!r}")
    return match.group("namespace"), match.group("name"), match.group("minimum")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.replace(temp_path, path)


def _cached_archive(
    client: PackageClient, cache_directory: Path, metadata: dict[str, Any]
) -> tuple[Path, str]:
    full_name = metadata.get("full_name")
    url = metadata.get("download_url")
    if not isinstance(full_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", full_name):
        raise ModpackError("Package has an unsafe full_name")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ModpackError(f"Package has an unsafe download URL: {url!r}")

    cache_path = cache_directory / f"{full_name}.zip"
    if not cache_path.exists():
        _atomic_write(cache_path, client.download(url))
    try:
        with ZipFile(cache_path) as archive:
            if archive.testzip() is not None:
                raise ModpackError(f"Corrupt package archive: {cache_path.name}")
    except BadZipFile as error:
        raise ModpackError(f"Package is not a ZIP archive: {cache_path.name}") from error
    with cache_path.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    return cache_path, digest


def resolve(
    manifest: list[ManifestPackage], client: PackageClient, cache_directory: Path
) -> dict[str, Any]:
    """Resolve manifest + dependencies and checksum all exact downloaded archives."""

    requirements: dict[tuple[str, str], Requirement] = {}
    queue: deque[tuple[str, str]] = deque()
    known_namespaces: dict[str, str] = {}

    def require(
        namespace: str,
        name: str,
        *,
        exact_version: str | None,
        minimum_version: str | None,
        role: str,
        allow_prerelease: bool,
    ) -> None:
        name_key = name.lower()
        prior_namespace = known_namespaces.setdefault(name_key, namespace)
        if prior_namespace != namespace:
            raise ModpackError(
                f"Source-ambiguous package name {name!r}: "
                f"{prior_namespace!r} and {namespace!r}"
            )
        key = namespace, name
        requirement = requirements.get(key)
        if requirement is None:
            requirement = Requirement(namespace, name)
            requirements[key] = requirement
            queue.append(key)
        if exact_version is not None:
            requirement.exact_versions.add(exact_version)
        if minimum_version is not None:
            requirement.minimum_versions.add(minimum_version)
        requirement.roles.add(role)
        requirement.allow_prerelease |= allow_prerelease

    for package in manifest:
        require(
            package.namespace,
            package.name,
            exact_version=None if package.version == "latest" else package.version,
            minimum_version=None,
            role=package.role,
            allow_prerelease=package.allow_prerelease,
        )

    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    while queue:
        key = queue.popleft()
        requirement = requirements[key]
        package = client.get_package(*key)
        _package_identity(package, key)
        if package.get("is_deprecated") is True:
            raise ModpackError(f"Package is deprecated: {key[0]}-{key[1]}")
        if len(requirement.exact_versions) > 1:
            raise ModpackError(
                f"Conflicting exact versions for {key[0]}-{key[1]}: "
                f"{sorted(requirement.exact_versions)}"
            )
        if requirement.exact_versions:
            selected_version = next(iter(requirement.exact_versions))
            metadata = client.get_version(*key, selected_version)
        else:
            metadata = package.get("latest")
            if not isinstance(metadata, dict):
                raise ModpackError(f"Package has no latest version: {key[0]}-{key[1]}")
            selected_version = metadata.get("version_number")
            if not isinstance(selected_version, str):
                raise ModpackError(f"Package latest version is invalid: {key[0]}-{key[1]}")

        _metadata_version(metadata, key, selected_version)
        if is_prerelease(selected_version) and not requirement.allow_prerelease:
            raise ModpackError(
                f"Prerelease package is not allowed: {key[0]}-{key[1]}-{selected_version}"
            )
        resolved[key] = metadata
        for dependency in metadata["dependencies"]:
            namespace, name, minimum = _parse_dependency(dependency)
            require(
                namespace,
                name,
                exact_version=None,
                minimum_version=minimum,
                role=requirement.role,
                allow_prerelease=False,
            )

    packages: list[dict[str, Any]] = []
    for key in sorted(resolved, key=lambda item: (item[0].lower(), item[1].lower())):
        requirement = requirements[key]
        metadata = resolved[key]
        version = metadata["version_number"]
        for minimum in requirement.minimum_versions:
            if not at_least(version, minimum):
                raise ModpackError(
                    f"{key[0]}-{key[1]}-{version} does not satisfy dependency "
                    f"minimum version {minimum}"
                )
        archive_path, digest = _cached_archive(client, cache_directory, metadata)
        packages.append(
            {
                "source": "hexium",
                "namespace": key[0],
                "name": key[1],
                "version": version,
                "full_name": metadata["full_name"],
                "download_url": metadata["download_url"],
                "sha256": digest,
                "dependencies": sorted(metadata["dependencies"]),
                "role": requirement.role,
                "cache_path": str(archive_path),
            }
        )
    return {"schema_version": 1, "source": "hexium", "packages": packages}


def write_lock(path: Path, lock: dict[str, Any]) -> None:
    """Atomically publish the successful resolution."""

    _atomic_write(path, (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode())
