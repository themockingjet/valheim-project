"""Operator CLI for the initial resolver milestone."""

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .deployment import activate_release, load_lock, rollback, stage_release
from .errors import ModpackError
from .hexium import HexiumClient
from .manifest import load_manifest
from .manifest_apply import apply_pending_manifest
from .resolver import resolve, write_lock

DEFAULT_ROOT = Path("/opt/valheim/modpack")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _resolve_command(arguments: argparse.Namespace) -> int:
    lock_file = _with_lock(arguments.root)
    try:
        manifest = load_manifest(arguments.manifest)
        lock = resolve(manifest, HexiumClient(), arguments.cache)
        write_lock(arguments.lock, lock)
        print(f"Resolved {len(lock['packages'])} Hexium package(s) into {arguments.lock}")
        return 0
    finally:
        lock_file.close()


def _with_lock(root: Path):
    lock_path = root / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_file.close()
        raise ModpackError("Another modpack operation is already running") from error
    return lock_file


def _assert_server_stopped() -> None:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", "valheim.service"],
        check=False,
    )
    if result.returncode == 0:
        raise ModpackError(
            "Refusing to activate while valheim.service is running; use the maintenance service"
        )


def _stage_command(arguments: argparse.Namespace, *, activate: bool) -> int:
    lock_file = _with_lock(arguments.root)
    try:
        if activate and not arguments.allow_active:
            _assert_server_stopped()
        packages = load_lock(arguments.lock)
        release = stage_release(
            packages,
            releases_directory=arguments.root / "releases",
            cache_directory=arguments.cache,
            config_overrides=arguments.root / "config-overrides",
            client=HexiumClient(),
        )
        if activate:
            activate_release(arguments.root, release)
            print(f"Activated release {release.name}")
        else:
            print(f"Staged release {release}")
        return 0
    finally:
        lock_file.close()


def _update_command(arguments: argparse.Namespace) -> int:
    return _stage_command(arguments, activate=True)


def _stage_only_command(arguments: argparse.Namespace) -> int:
    return _stage_command(arguments, activate=False)


def _maintain_command(arguments: argparse.Namespace) -> int:
    """Resolve, stage, and activate under one lock for maintenance orchestration."""

    _assert_server_stopped()
    lock_file = _with_lock(arguments.root)
    try:
        manifest = load_manifest(arguments.manifest)
        lock = resolve(manifest, HexiumClient(), arguments.cache)
        write_lock(arguments.lock, lock)
        release = stage_release(
            load_lock(arguments.lock),
            releases_directory=arguments.root / "releases",
            cache_directory=arguments.cache,
            config_overrides=arguments.root / "config-overrides",
            client=HexiumClient(),
        )
        activate_release(arguments.root, release)
        print(f"Activated release {release.name}")
        return 0
    finally:
        lock_file.close()


def _rollback_command(arguments: argparse.Namespace) -> int:
    lock_file = _with_lock(arguments.root)
    try:
        if not arguments.allow_active:
            _assert_server_stopped()
        release = rollback(arguments.root)
        print(f"Rolled back to {release.name}")
        return 0
    finally:
        lock_file.close()


def _status_command(arguments: argparse.Namespace) -> int:
    current = arguments.root / "current"
    if not current.is_symlink():
        print(json.dumps({"active_release": None}, sort_keys=True))
        return 0
    release = current.resolve(strict=True)
    release_lock = release / "release-lock.json"
    if not release_lock.is_file():
        raise ModpackError("current does not point to a valid release")
    print(
        json.dumps(
            {"active_release": release.name, "release": json.loads(release_lock.read_text())},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _apply_pending_manifest_command(arguments: argparse.Namespace) -> int:
    """Validate and publish one dashboard-originated pending manifest request.

    This never stops, starts, or restarts valheim.service, and never
    activates a release; it only replaces the reviewed manifest used by the
    next scheduled maintenance run.
    """

    lock_file = _with_lock(arguments.root)
    try:
        try:
            request = json.loads(arguments.request.read_text(encoding="utf-8"))
        except OSError as error:
            raise ModpackError(f"Cannot read pending manifest request: {error}") from error
        except json.JSONDecodeError as error:
            raise ModpackError(f"Pending manifest request is not valid JSON: {error}") from error

        result = apply_pending_manifest(
            request,
            manifest_path=arguments.manifest,
            lock_path=arguments.lock,
            cache_directory=arguments.cache,
            client=HexiumClient(),
        )
        print(json.dumps(result.to_dict(), sort_keys=True))
        return 0
    finally:
        lock_file.close()


def _plan_command(arguments: argparse.Namespace) -> int:
    """Resolve in temporary storage and report package changes without persistence."""

    with tempfile.TemporaryDirectory(prefix="valheim-modpack-plan-") as directory:
        candidate = resolve(
            load_manifest(arguments.manifest),
            HexiumClient(),
            Path(directory) / "cache",
        )
    candidate_names = {
        f"{package['namespace']}-{package['name']}-{package['version']}"
        for package in candidate["packages"]
    }
    active_names: set[str] = set()
    if arguments.lock.exists():
        active_names = {package.full_name for package in load_lock(arguments.lock)}
    print(
        json.dumps(
            {
                "add": sorted(candidate_names - active_names),
                "remove": sorted(active_names - candidate_names),
                "unchanged": sorted(candidate_names & active_names),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _gc_command(arguments: argparse.Namespace) -> int:
    """Retain active/previous plus a bounded number of newest release snapshots."""

    lock_file = _with_lock(arguments.root)
    try:
        releases_directory = arguments.root / "releases"
        if not releases_directory.exists():
            print(json.dumps({"removed_releases": [], "removed_cache": []}))
            return 0
        protected = set()
        for name in ("current", "previous"):
            link = arguments.root / name
            if link.is_symlink():
                protected.add(link.resolve(strict=True))
        releases = sorted(
            (path for path in releases_directory.iterdir() if path.is_dir() and not path.is_symlink()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        retained = set(releases[: arguments.keep]) | protected
        removed_releases = []
        for release in releases:
            if release in retained:
                continue
            shutil.rmtree(release)
            removed_releases.append(release.name)

        referenced_archives = set()
        if arguments.lock.exists():
            referenced_archives.update(
                f"{package.full_name}.zip" for package in load_lock(arguments.lock)
            )
        for release in retained:
            release_lock = release / "release-lock.json"
            if not release_lock.is_file():
                continue
            try:
                package_names = json.loads(release_lock.read_text(encoding="utf-8"))["packages"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise ModpackError(f"Cannot read release lock {release_lock}: {error}") from error
            if not all(isinstance(name, str) for name in package_names):
                raise ModpackError(f"Release lock has invalid package names: {release_lock}")
            referenced_archives.update(f"{name}.zip" for name in package_names)
        removed_cache = []
        if arguments.cache.exists():
            for archive in arguments.cache.glob("*.zip"):
                if archive.name in referenced_archives:
                    continue
                archive.unlink()
                removed_cache.append(archive.name)
        print(
            json.dumps(
                {
                    "removed_releases": sorted(removed_releases),
                    "removed_cache": sorted(removed_cache),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        lock_file.close()


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small first-milestone CLI."""

    parser = argparse.ArgumentParser(prog="valheim-modpack")
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser(
        "resolve", help="resolve a Hexium manifest into a checksummed lock file"
    )
    resolve_parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_ROOT / "manifest.yaml"
    )
    resolve_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    resolve_parser.add_argument(
        "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
    )
    resolve_parser.add_argument(
        "--cache", type=Path, default=DEFAULT_ROOT / "cache"
    )
    resolve_parser.set_defaults(handler=_resolve_command)
    maintain_parser = subparsers.add_parser(
        "maintain",
        help="resolve, stage, and activate while the maintenance service has stopped Valheim",
    )
    maintain_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    maintain_parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_ROOT / "manifest.yaml"
    )
    maintain_parser.add_argument(
        "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
    )
    maintain_parser.add_argument("--cache", type=Path, default=DEFAULT_ROOT / "cache")
    maintain_parser.set_defaults(handler=_maintain_command)
    for command, handler, help_text in (
        ("stage", _stage_only_command, "stage a locked release without activating it"),
        ("update", _update_command, "stage and activate a locked release"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        command_parser.add_argument(
            "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
        )
        command_parser.add_argument("--cache", type=Path, default=DEFAULT_ROOT / "cache")
        command_parser.add_argument(
            "--allow-active",
            action="store_true",
            help="override the stopped-server activation guard for controlled tests only",
        )
        command_parser.set_defaults(handler=handler)
    rollback_parser = subparsers.add_parser(
        "rollback", help="activate the previous known-good release"
    )
    rollback_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    rollback_parser.add_argument("--allow-active", action="store_true")
    rollback_parser.set_defaults(handler=_rollback_command)
    status_parser = subparsers.add_parser("status", help="show active release state")
    status_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    status_parser.set_defaults(handler=_status_command)
    plan_parser = subparsers.add_parser(
        "plan", help="resolve a candidate and show package changes without writing a lock"
    )
    plan_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    plan_parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_ROOT / "manifest.yaml"
    )
    plan_parser.add_argument(
        "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
    )
    plan_parser.set_defaults(handler=_plan_command)
    apply_pending_manifest_parser = subparsers.add_parser(
        "apply-pending-manifest",
        help="validate and publish one dashboard-originated pending manifest request",
    )
    apply_pending_manifest_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    apply_pending_manifest_parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_ROOT / "manifest.yaml"
    )
    apply_pending_manifest_parser.add_argument(
        "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
    )
    apply_pending_manifest_parser.add_argument("--cache", type=Path, default=DEFAULT_ROOT / "cache")
    apply_pending_manifest_parser.add_argument("--request", type=Path, required=True)
    apply_pending_manifest_parser.set_defaults(handler=_apply_pending_manifest_command)
    gc_parser = subparsers.add_parser("gc", help="prune unreferenced archives and old releases")
    gc_parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    gc_parser.add_argument("--cache", type=Path, default=DEFAULT_ROOT / "cache")
    gc_parser.add_argument(
        "--lock", type=Path, default=DEFAULT_ROOT / "modpack.lock.json"
    )
    gc_parser.add_argument("--keep", type=_positive_int, default=3)
    gc_parser.set_defaults(handler=_gc_command)
    return parser


def main() -> None:
    """Run the operator CLI with concise, actionable validation failures."""

    arguments = build_parser().parse_args()
    try:
        exit_code = arguments.handler(arguments)
    except ModpackError as error:
        print(f"valheim-modpack: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    raise SystemExit(exit_code)
