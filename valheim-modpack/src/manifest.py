"""Validated manifest model."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

import yaml

from .errors import ModpackError
from .versioning import parse_version

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
Role = Literal["server", "client"]


@dataclass(frozen=True)
class ManifestPackage:
    """One explicitly requested package."""

    namespace: str
    name: str
    version: str
    role: Role
    allow_prerelease: bool

    @property
    def key(self) -> tuple[str, str]:
        return self.namespace, self.name


def _require_string(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise ModpackError(f"packages[{index}].{field} must be a non-empty string")
    return value


def load_manifest(path: Path) -> list[ManifestPackage]:
    """Load and validate a reviewed manifest without accepting unknown sources."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ModpackError(f"Cannot read manifest {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ModpackError(f"Cannot parse manifest {path}: {error}") from error

    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModpackError("manifest schema_version must be 1")
    packages = raw.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ModpackError("manifest packages must be a non-empty list")

    result: list[ManifestPackage] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(packages):
        if not isinstance(item, dict):
            raise ModpackError(f"packages[{index}] must be a mapping")
        if item.get("source") != "hexium":
            raise ModpackError(
                f"packages[{index}].source must be 'hexium'; other sources are unsupported"
            )

        namespace = _require_string(item.get("namespace"), "namespace", index)
        name = _require_string(item.get("name"), "name", index)
        version = _require_string(item.get("version"), "version", index)
        channel = item.get("channel", "stable")
        role = item.get("role", "server")

        if not IDENTIFIER_PATTERN.fullmatch(namespace):
            raise ModpackError(f"packages[{index}].namespace is invalid: {namespace!r}")
        if not PACKAGE_NAME_PATTERN.fullmatch(name):
            raise ModpackError(f"packages[{index}].name is invalid: {name!r}")
        if version != "latest":
            parse_version(version)
        if channel not in {"stable", "prerelease"}:
            raise ModpackError(
                f"packages[{index}].channel must be 'stable' or 'prerelease'"
            )
        if role not in {"server", "client"}:
            raise ModpackError(f"packages[{index}].role must be 'server' or 'client'")

        package = ManifestPackage(
            namespace=namespace,
            name=name,
            version=version,
            role=role,
            allow_prerelease=channel == "prerelease",
        )
        if package.key in seen:
            raise ModpackError(
                f"Duplicate manifest package: {package.namespace}-{package.name}"
            )
        seen.add(package.key)
        result.append(package)
    return result
