"""Apply a dashboard-originated pending manifest request to the reviewed manifest.

This module is the only bridge between the unprivileged dashboard's pending
directory-job transport and the modpack's authoritative manifest file. It
re-validates the request independently of the dashboard (defense in depth),
confirms the candidate manifest actually resolves against Hexium, and only
then atomically replaces the manifest used by the next scheduled maintenance
run. It never stops, starts, or restarts `valheim.service`, and never
activates a release; that remains the exclusive responsibility of the
existing twice-daily maintenance orchestration.
"""

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol

import yaml

from .errors import ModpackError
from .deployment import load_lock
from .manifest import load_manifest
from .resolver import resolve

MAX_PACKAGES = 50
MAX_TIMESTAMP_CHARACTERS = 40
REQUIRED_REQUEST_KEYS = {"schema_version", "requested_at", "packages"}
REQUIRED_PACKAGE_KEYS = {"source", "namespace", "name", "version", "channel", "role"}


class ResolverClient(Protocol):
    def get_package(self, namespace: str, name: str) -> dict[str, Any]: ...

    def get_version(self, namespace: str, name: str, version: str) -> dict[str, Any]: ...

    def download(self, url: str) -> bytes: ...


@dataclass(frozen=True)
class ManifestApplyResult:
    """Bounded, non-sensitive summary safe to hand back to the dashboard."""

    outcome: str
    package_count: int
    add: list[str]
    remove: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "package_count": self.package_count,
            "add": self.add,
            "remove": self.remove,
        }


def _require_request_shape(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ModpackError("pending manifest request must be an object")
    if set(request) != REQUIRED_REQUEST_KEYS:
        raise ModpackError("pending manifest request has an unsupported shape")
    if request["schema_version"] != 1:
        raise ModpackError("pending manifest request schema_version is unsupported")
    requested_at = request["requested_at"]
    if not isinstance(requested_at, str) or len(requested_at) > MAX_TIMESTAMP_CHARACTERS:
        raise ModpackError("pending manifest request timestamp is invalid")
    try:
        datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ModpackError("pending manifest request timestamp is invalid") from error
    packages = request["packages"]
    if not isinstance(packages, list) or not packages or len(packages) > MAX_PACKAGES:
        raise ModpackError("pending manifest request packages are invalid")
    for index, package in enumerate(packages):
        if not isinstance(package, dict) or set(package) != REQUIRED_PACKAGE_KEYS:
            raise ModpackError(f"pending manifest request packages[{index}] is invalid")
        if package["source"] != "hexium":
            raise ModpackError(f"pending manifest request packages[{index}].source is invalid")
    return request


def _build_manifest_document(packages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "packages": [
            {
                "source": "hexium",
                "namespace": package["namespace"],
                "name": package["name"],
                "version": package["version"],
                "channel": package["channel"],
                "role": package["role"],
            }
            for package in packages
        ],
    }


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.chmod(temp_path, 0o644)
    os.replace(temp_path, path)


def apply_pending_manifest(
    request: object,
    *,
    manifest_path: Path,
    lock_path: Path,
    cache_directory: Path,
    client: ResolverClient,
) -> ManifestApplyResult:
    """Validate, resolve, and atomically publish one pending manifest request.

    Raises ``ModpackError`` for any malformed request or a manifest that does
    not resolve; callers must treat that as a rejected request and must not
    modify ``manifest_path``. ``lock_path`` is the existing active lock file
    (if any), used only to report a bounded add/remove preview; it is never
    written by this function.
    """

    validated = _require_request_shape(request)
    document = _build_manifest_document(validated["packages"])
    document_text = yaml.safe_dump(document, sort_keys=False)

    with tempfile.TemporaryDirectory(prefix="valheim-modpack-manifest-apply-") as directory:
        staged_manifest = Path(directory) / "manifest.yaml"
        staged_manifest.write_text(document_text, encoding="utf-8")
        # Reuse the authoritative manifest loader so this path can never
        # diverge from what `resolve`/`maintain` will accept later. Resolve
        # against the persistent cache so this does not re-download archives
        # that a later `maintain` run will also need.
        candidate_packages = load_manifest(staged_manifest)
        candidate_lock = resolve(candidate_packages, client, cache_directory)

    previous_names: set[str] = set()
    if lock_path.exists():
        try:
            previous_names = {package.full_name for package in load_lock(lock_path)}
        except ModpackError:
            previous_names = set()

    candidate_names = {
        f"{package['namespace']}-{package['name']}-{package['version']}"
        for package in candidate_lock["packages"]
    }

    _atomic_write_text(manifest_path, document_text)

    return ManifestApplyResult(
        outcome="success",
        package_count=len(validated["packages"]),
        add=sorted(candidate_names - previous_names),
        remove=sorted(previous_names - candidate_names),
    )
