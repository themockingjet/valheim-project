"""Bounded, atomically-written audit trail for state-changing dashboard actions."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

MAX_AUDIT_EVENTS = 200
MAX_DETAIL_CHARACTERS = 200
MAX_AUDIT_BYTES = 256 * 1024
AUDIT_FILE_NAME = "audit.json"

ALLOWED_ACTIONS = {
    "manifest_submit",
    "update_request",
    "rollback_request",
    "world_restore_request",
}
ALLOWED_OUTCOMES = {"accepted", "rejected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(body)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.chmod(temp_path, 0o640)
    os.replace(temp_path, path)


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as audit_file:
            contents = audit_file.read(MAX_AUDIT_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    if len(contents) > MAX_AUDIT_BYTES:
        return []
    try:
        events = json.loads(contents.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def record_event(
    audit_directory: Path,
    *,
    action: str,
    outcome: str,
    detail: str = "",
    remote_address: str | None = None,
) -> None:
    """Append one bounded audit event, trimming the oldest once full."""

    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported audit action: {action!r}")
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError(f"unsupported audit outcome: {outcome!r}")

    audit_path = audit_directory / AUDIT_FILE_NAME
    events = _read_events(audit_path)
    events.append(
        {
            "recorded_at": _now_iso(),
            "action": action,
            "outcome": outcome,
            "detail": detail[:MAX_DETAIL_CHARACTERS],
            "remote_address": (remote_address or "")[:64],
        }
    )
    if len(events) > MAX_AUDIT_EVENTS:
        events = events[-MAX_AUDIT_EVENTS:]
    _atomic_write_json(audit_path, events)


def read_recent_events(audit_directory: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent bounded audit events, newest last."""

    audit_path = audit_directory / AUDIT_FILE_NAME
    events = _read_events(audit_path)
    bounded_limit = max(0, min(limit, MAX_AUDIT_EVENTS))
    return events[-bounded_limit:] if bounded_limit else []
