#!/usr/bin/env python3
"""Build and atomically publish the bounded dashboard status snapshot."""

from collections import deque
from datetime import datetime, timezone
import argparse
import grp
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, Sequence

from src.config_overrides import publish_config_export
from src.errors import ModpackError
from src.manifest import load_manifest

DEFAULT_MODPACK_ROOT = Path("/opt/valheim/modpack")
DEFAULT_OUTPUT = Path("/var/lib/valheim-dashboard/exports/status.json")
DEFAULT_CONFIG_OUTPUT = Path("/var/lib/valheim-dashboard/exports/mod-configs.json")
DEFAULT_MAINTENANCE_STATE = Path("/var/lib/valheim-dashboard/maintenance-state.json")
SERVER_LOG = Path("/opt/valheim/logs/valheim.log")
MAINTENANCE_UNIT = "valheim-restart.service"
MAX_PACKAGES = 100
MAX_LOG_ENTRIES = 100
MAX_LOG_ENTRY_CHARACTERS = 500
MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_INPUT_BYTES = 256 * 1024
MAX_IDENTIFIER_CHARACTERS = 128
LOG_READ_BYTES = 256 * 1024

SYSTEMCTL_SERVICE_ARGS = (
    "/usr/bin/systemctl",
    "show",
    "valheim.service",
    "--property=ActiveState,SubState,MainPID,ActiveEnterTimestampMonotonic,"
    "MemoryCurrent,CPUUsageNSec",
    "--no-pager",
)
SYSTEMCTL_TIMER_ARGS = (
    "/usr/bin/systemctl",
    "show",
    "valheim-restart.timer",
    "--property=NextElapseUSecRealtime",
    "--timestamp=unix",
    "--no-pager",
)
JOURNAL_ARGS = (
    "/usr/bin/journalctl",
    "--unit=valheim-restart.service",
    "--no-pager",
    "--output=cat",
    "--lines=100",
)
SERVER_JOURNAL_ARGS = (
    "/usr/bin/journalctl",
    "--unit=valheim.service",
    "--no-pager",
    "--output=cat",
    "--lines=100",
)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
URL_PATTERN = re.compile(r"\b(?:https?|ftp)://[^\s]+", re.IGNORECASE)
CHECKSUM_PATTERN = re.compile(r"\b[0-9a-fA-F]{64}\b")
SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|credential|api[_-]?key)\b\s*[:=]\s*\S+"
)


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _run_fixed(
    arguments: Sequence[str], *, timeout: float = 3.0
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _bounded_text(path: Path, maximum: int = MAX_INPUT_BYTES) -> str | None:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            start = max(0, source.tell() - maximum)
            source.seek(start)
            return source.read(maximum).decode("utf-8", errors="replace")
    except OSError:
        return None


def _redact_log_line(line: str) -> str:
    line = URL_PATTERN.sub("[redacted-url]", line)
    line = CHECKSUM_PATTERN.sub("[redacted-checksum]", line)
    return SECRET_PATTERN.sub(r"\1=[redacted]", line)


def _read_log(path: Path) -> list[str]:
    contents = _bounded_text(path)
    if contents is None:
        return []
    lines = deque(contents.splitlines(), maxlen=MAX_LOG_ENTRIES)
    return [
        _redact_log_line(line)[:MAX_LOG_ENTRY_CHARACTERS]
        for line in lines
    ]


def _command_lines(arguments: Sequence[str]) -> list[str]:
    result = _run_fixed(arguments)
    if result is None or result.returncode != 0:
        return []
    return result.stdout.splitlines()[-MAX_LOG_ENTRIES:]


def _server_log_lines(path: Path) -> list[str]:
    lines = _read_log(path)
    if lines or path != SERVER_LOG:
        return lines
    return [
        _redact_log_line(line)[:MAX_LOG_ENTRY_CHARACTERS]
        for line in _command_lines(SERVER_JOURNAL_ARGS)
    ]


def _command_properties(arguments: Sequence[str]) -> dict[str, str]:
    result = _run_fixed(arguments)
    if result is None or result.returncode != 0:
        return {}
    return {
        name: value
        for line in result.stdout.splitlines()
        if (parts := line.split("=", 1)) and len(parts) == 2
        for name, value in [parts]
    }


def _nonnegative_int(value: str) -> int | None:
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _service_state() -> dict[str, Any]:
    values = _command_properties(SYSTEMCTL_SERVICE_ARGS)
    active_state = values.get("ActiveState", "").strip()[:32] or "unknown"
    sub_state = values.get("SubState", "").strip()[:32] or "unknown"
    pid_value = _nonnegative_int(values.get("MainPID", ""))
    active_enter = _nonnegative_int(values.get("ActiveEnterTimestampMonotonic", ""))
    memory = _nonnegative_int(values.get("MemoryCurrent", ""))
    cpu = _nonnegative_int(values.get("CPUUsageNSec", ""))
    pid = pid_value if pid_value else None
    uptime = None
    if pid is not None and active_enter is not None and active_state == "active":
        uptime = round(max(0.0, time.monotonic() - active_enter / 1_000_000), 3)

    return {
        "active_state": active_state,
        "sub_state": sub_state,
        "pid": pid,
        "uptime_seconds": uptime,
        "memory_current_bytes": memory,
        "cpu_usage_ns": cpu,
    }


def _next_scheduled_at() -> str | None:
    timestamp_value = _command_properties(SYSTEMCTL_TIMER_ARGS).get(
        "NextElapseUSecRealtime", ""
    )
    if not timestamp_value.startswith("@"):
        return None
    timestamp = _nonnegative_int(timestamp_value[1:])
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _valid_release(root: Path, link_name: str) -> Path | None:
    link = root / link_name
    if not link.is_symlink():
        return None
    try:
        resolved = link.resolve(strict=True)
        releases = (root / "releases").resolve(strict=True)
        resolved.relative_to(releases)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_dir() else None


def _read_json(path: Path) -> object | None:
    contents = _bounded_text(path)
    if contents is None:
        return None
    try:
        return json.loads(contents)
    except json.JSONDecodeError:
        return None


def _release_packages(root: Path, release: Path | None) -> list[dict[str, str]]:
    if release is None:
        return []
    release_lock = _read_json(release / "release-lock.json")
    lock = _read_json(root / "modpack.lock.json")
    if not isinstance(release_lock, dict) or not isinstance(lock, dict):
        return []
    names = release_lock.get("packages")
    entries = lock.get("packages")
    if not isinstance(names, list) or not isinstance(entries, list):
        return []

    by_full_name: dict[str, dict[str, object]] = {}
    for entry in entries[:MAX_PACKAGES]:
        if not isinstance(entry, dict):
            continue
        full_name = entry.get("full_name")
        if isinstance(full_name, str) and SAFE_IDENTIFIER.fullmatch(full_name):
            by_full_name[full_name] = entry

    packages: list[dict[str, str]] = []
    for full_name in names:
        if not isinstance(full_name, str) or full_name not in by_full_name:
            continue
        entry = by_full_name[full_name]
        namespace = entry.get("namespace")
        name = entry.get("name")
        version = entry.get("version")
        role = entry.get("role")
        if not all(
            isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value)
            for value in (namespace, name, version)
        ) or role not in {"server", "client"}:
            continue
        packages.append(
            {
                "namespace": namespace,
                "name": name,
                "version": version,
                "role": role,
            }
        )
        if len(packages) >= MAX_PACKAGES:
            break
    return packages


def _manifest_packages(path: Path) -> list[dict[str, str]] | None:
    try:
        packages = load_manifest(path)
    except ModpackError:
        return None
    return [
        {
            "namespace": package.namespace,
            "name": package.name,
            "version": package.version,
            "channel": "prerelease" if package.allow_prerelease else "stable",
            "role": package.role,
        }
        for package in packages
    ]


def _default_maintenance_state() -> dict[str, Any]:
    return {
        "outcome": "success",
        "started_at": None,
        "completed_at": None,
        "rollback_performed": False,
        "failure_reason": None,
    }


def _load_maintenance_state(path: Path) -> dict[str, Any]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return _default_maintenance_state()
    result = _default_maintenance_state()
    if raw.get("outcome") in {"running", "success", "failed", "rolled_back"}:
        result["outcome"] = raw["outcome"]
    for field in ("started_at", "completed_at", "failure_reason"):
        value = raw.get(field)
        if value is None or isinstance(value, str):
            result[field] = value[:500] if isinstance(value, str) else None
    if isinstance(raw.get("rollback_performed"), bool):
        result["rollback_performed"] = raw["rollback_performed"]
    return result


def _atomic_write(
    path: Path,
    contents: bytes,
    *,
    mode: int,
    group_name: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary_path: Path | None = None
    descriptor = -1
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".new"
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, mode)
        if group_name is not None:
            os.fchown(descriptor, 0, grp.getgrnam(group_name).gr_gid)
        with os.fdopen(descriptor, "wb") as temporary:
            descriptor = -1
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _update_maintenance_state(
    path: Path,
    outcome: str,
    *,
    failure_reason: str | None = None,
    rollback_performed: bool = False,
) -> dict[str, Any]:
    state = _load_maintenance_state(path)
    now = _iso_now()
    if outcome == "running":
        state = {
            "outcome": "running",
            "started_at": now,
            "completed_at": None,
            "rollback_performed": False,
            "failure_reason": None,
        }
    else:
        state["outcome"] = outcome
        state["completed_at"] = now
        state["rollback_performed"] = rollback_performed
        state["failure_reason"] = failure_reason[:500] if failure_reason else None
    _atomic_write(
        path,
        (json.dumps(state, separators=(",", ":")) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return state


def _fit_snapshot(snapshot: dict[str, Any]) -> bytes:
    def encode() -> bytes:
        return (json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    encoded = encode()
    while len(encoded) > MAX_SNAPSHOT_BYTES:
        server_logs = snapshot["logs"]["server"]
        maintenance_logs = snapshot["logs"]["maintenance"]
        if not server_logs and not maintenance_logs:
            raise ValueError("status snapshot exceeds the maximum size")
        if len(server_logs) >= len(maintenance_logs) and server_logs:
            server_logs.pop(0)
        elif maintenance_logs:
            maintenance_logs.pop(0)
        encoded = encode()
    return encoded


def build_snapshot(
    *,
    modpack_root: Path = DEFAULT_MODPACK_ROOT,
    server_log: Path = SERVER_LOG,
    maintenance_log: Path | None = None,
    maintenance_state: Path = DEFAULT_MAINTENANCE_STATE,
    generated_at: str | None = None,
) -> dict[str, Any]:
    current = _valid_release(modpack_root, "current")
    previous = _valid_release(modpack_root, "previous")
    maintenance_lines = (
        _read_log(maintenance_log)
        if maintenance_log is not None
        else [_redact_log_line(line)[:MAX_LOG_ENTRY_CHARACTERS] for line in _command_lines(JOURNAL_ARGS)]
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at or _iso_now(),
        "server": _service_state(),
        "maintenance": {
            "next_scheduled_at": _next_scheduled_at(),
            "last": _load_maintenance_state(maintenance_state),
        },
        "modpack": {
            "active_release": current.name if current is not None else None,
            "previous_release": previous.name if previous is not None else None,
            "packages": _release_packages(modpack_root, current),
            "manifest_packages": _manifest_packages(modpack_root / "manifest.yaml"),
        },
        "logs": {
            "server": _server_log_lines(server_log),
            "maintenance": maintenance_lines[-MAX_LOG_ENTRIES:],
        },
    }


def publish_snapshot(snapshot: dict[str, Any], output: Path = DEFAULT_OUTPUT) -> None:
    """Serialize and atomically publish a root-owned dashboard snapshot."""

    contents = _fit_snapshot(snapshot)
    group_name = "valheim-ui" if output == DEFAULT_OUTPUT else None
    _atomic_write(output, contents, mode=0o640, group_name=group_name)


def publish(
    *,
    output: Path = DEFAULT_OUTPUT,
    config_output: Path | None = None,
    modpack_root: Path = DEFAULT_MODPACK_ROOT,
    maintenance_state: Path = DEFAULT_MAINTENANCE_STATE,
) -> None:
    if output == DEFAULT_OUTPUT and os.geteuid() != 0:
        raise PermissionError("status exporter must run as root")
    publish_snapshot(
        build_snapshot(
            modpack_root=modpack_root,
            maintenance_state=maintenance_state,
        ),
        output,
    )
    if config_output is None and output == DEFAULT_OUTPUT:
        config_output = DEFAULT_CONFIG_OUTPUT
    if config_output is not None:
        group_id = (
            grp.getgrnam("valheim-ui").gr_gid
            if config_output == DEFAULT_CONFIG_OUTPUT
            else None
        )
        publish_config_export(modpack_root, config_output, group_id=group_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config-output", type=Path, default=DEFAULT_CONFIG_OUTPUT)
    parser.add_argument("--modpack-root", type=Path, default=DEFAULT_MODPACK_ROOT)
    parser.add_argument("--maintenance-state", type=Path, default=DEFAULT_MAINTENANCE_STATE)
    parser.add_argument(
        "--maintenance-outcome",
        choices=("running", "success", "failed", "rolled_back"),
    )
    parser.add_argument("--failure-reason")
    parser.add_argument("--rollback-performed", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if os.geteuid() != 0:
        raise SystemExit("status exporter must run as root")
    if arguments.maintenance_outcome:
        _update_maintenance_state(
            arguments.maintenance_state,
            arguments.maintenance_outcome,
            failure_reason=arguments.failure_reason,
            rollback_performed=arguments.rollback_performed,
        )
    publish(
        output=arguments.output,
        config_output=arguments.config_output,
        modpack_root=arguments.modpack_root,
        maintenance_state=arguments.maintenance_state,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
