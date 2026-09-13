"""Localhost-only HTTP server for the dashboard API and production frontend."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from . import audit, hexium_catalog, pending, world_backups
from .security import (
    RequestRejected,
    SessionStore,
    default_allowed_host,
    parse_session_cookie,
    require_safe_state_change,
    session_cookie_header,
)
from .status_snapshot import SnapshotError, load_snapshot

HOST = "127.0.0.1"
PORT = 8080
STATUS_EXPORT_PATH = Path("/var/lib/valheim-dashboard/exports/status.json")
PENDING_DIRECTORY = Path("/var/lib/valheim-dashboard/pending")
STATE_DIRECTORY = Path("/var/lib/valheim-dashboard/state")
AUDIT_DIRECTORY = Path("/var/lib/valheim-dashboard/audit")
WORLD_BACKUP_EXPORT_PATH = Path("/var/lib/valheim-dashboard/exports/world-backups.json")
MAX_REQUEST_BODY_BYTES = 64 * 1024
STATUS_EVENT_POLL_SECONDS = 2
STATUS_EVENT_HEARTBEAT_SECONDS = 15
STATUS_EVENT_MAX_SECONDS = 5 * 60

PENDING_ACTIONS = {
    "manifest": (
        pending.MANIFEST_REQUEST_FILE,
        pending.MANIFEST_RESULT_FILE,
        pending.build_manifest_request,
        "manifest_submit",
    ),
    "update": (
        pending.UPDATE_REQUEST_FILE,
        pending.UPDATE_RESULT_FILE,
        pending.build_update_request,
        "update_request",
    ),
    "rollback": (
        pending.ROLLBACK_REQUEST_FILE,
        pending.ROLLBACK_RESULT_FILE,
        pending.build_rollback_request,
        "rollback_request",
    ),
    "world-restore": (
        pending.WORLD_RESTORE_REQUEST_FILE,
        pending.WORLD_RESTORE_RESULT_FILE,
        pending.build_world_restore_request,
        "world_restore_request",
    ),
}


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the health API and, in production, trusted compiled frontend files."""

    server_version = "ValheimDashboard/0.0"
    sys_version = ""
    static_directory: Path | None = None
    status_path = STATUS_EXPORT_PATH
    pending_directory = PENDING_DIRECTORY
    state_directory = STATE_DIRECTORY
    audit_directory = AUDIT_DIRECTORY
    world_backup_path = WORLD_BACKUP_EXPORT_PATH
    package_catalog: hexium_catalog.HexiumCatalog = hexium_catalog.HexiumCatalog()
    allowed_host = HOST + ":" + str(PORT)
    session_store: SessionStore = SessionStore()
    pending_lock = threading.Lock()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/healthz", "/api/healthz"}:
            self._send_health()
            return
        status_sections = {
            "/api/status": None,
            "/api/server-status": "server",
            "/api/maintenance-status": "maintenance",
            "/api/modpack-status": "modpack",
        }
        if path in status_sections:
            self._send_status(status_sections[path])
            return
        if path == "/api/session":
            self._send_session()
            return
        if path == "/api/audit":
            self._send_audit()
            return
        if path == "/api/world-backups":
            self._send_world_backups()
            return
        if path == "/api/hexium/search":
            self._send_hexium_search(urlsplit(self.path).query)
            return
        if path.startswith("/api/hexium/package/"):
            self._send_hexium_package(path)
            return
        if path == "/api/status/events":
            self._stream_status_events()
            return
        if path in {
            "/api/pending/manifest",
            "/api/pending/update",
            "/api/pending/rollback",
            "/api/pending/world-restore",
        }:
            self._send_pending_status(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/"):
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "API route was not found"}},
            )
            return
        if self.static_directory is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_static(path)

    def do_DELETE(self) -> None:
        self._send_method_not_allowed()

    def do_HEAD(self) -> None:
        self._send_method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._send_method_not_allowed()

    def do_PATCH(self) -> None:
        self._send_method_not_allowed()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path in {
            "/api/pending/manifest",
            "/api/pending/update",
            "/api/pending/rollback",
            "/api/pending/world-restore",
        }:
            self._handle_pending_submit(path.rsplit("/", 1)[-1])
            return
        self._send_method_not_allowed()

    def do_PUT(self) -> None:
        self._send_method_not_allowed()

    def _send_health(self) -> None:
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def _send_status(self, section: str | None) -> None:
        try:
            snapshot = load_snapshot(self.status_path)
        except SnapshotError as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": error.code, "message": str(error)}},
            )
            return
        if section is not None:
            snapshot = {
                "schema_version": snapshot["schema_version"],
                "generated_at": snapshot["generated_at"],
                section: snapshot[section],
            }
        self._send_json(HTTPStatus.OK, snapshot)

    def _status_fingerprint(self) -> tuple[int, int] | None:
        """Return non-sensitive metadata used only to detect export changes."""

        try:
            status = self.status_path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(status.st_mode):
            return None
        return (status.st_mtime_ns, status.st_size)

    def _stream_status_events(self) -> None:
        """Signal snapshot replacement without streaming snapshot contents."""

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        fingerprint = self._status_fingerprint()
        deadline = time.monotonic() + STATUS_EVENT_MAX_SECONDS
        heartbeat_at = time.monotonic() + STATUS_EVENT_HEARTBEAT_SECONDS
        try:
            self._write_status_event("connected")
            while time.monotonic() < deadline:
                time.sleep(STATUS_EVENT_POLL_SECONDS)
                current_fingerprint = self._status_fingerprint()
                if current_fingerprint != fingerprint:
                    fingerprint = current_fingerprint
                    self._write_status_event("snapshot")
                    heartbeat_at = time.monotonic() + STATUS_EVENT_HEARTBEAT_SECONDS
                elif time.monotonic() >= heartbeat_at:
                    self._write_status_event("heartbeat")
                    heartbeat_at = time.monotonic() + STATUS_EVENT_HEARTBEAT_SECONDS
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_status_event(self, event: str) -> None:
        self.wfile.write(f"event: {event}\ndata: {{}}\n\n".encode("ascii"))
        self.wfile.flush()

    def _send_session(self) -> None:
        session_id = parse_session_cookie(self.headers.get("Cookie"))
        session = self.session_store.get(session_id)
        set_cookie = None
        if session is None:
            session = self.session_store.create()
            set_cookie = session_cookie_header(session.session_id)
        self._send_json(
            HTTPStatus.OK,
            {"csrf_token": session.csrf_token},
            set_cookie=set_cookie,
        )

    def _send_audit(self) -> None:
        events = audit.read_recent_events(self.audit_directory)
        self._send_json(HTTPStatus.OK, {"events": events})

    def _send_world_backups(self) -> None:
        try:
            inventory = world_backups.load_inventory(self.world_backup_path)
        except world_backups.WorldBackupError as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": error.code, "message": str(error)}},
            )
            return
        self._send_json(HTTPStatus.OK, inventory)

    def _send_hexium_search(self, query_string: str) -> None:
        parameters = parse_qs(query_string, keep_blank_values=True)
        if set(parameters) != {"q"} or len(parameters["q"]) != 1:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_search", "message": "A single search query is required"}},
            )
            return
        try:
            results = self.package_catalog.search(parameters["q"][0])
        except hexium_catalog.InvalidSearch as error:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_search", "message": str(error)}},
            )
            return
        except hexium_catalog.CatalogError as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": "hexium_unavailable", "message": str(error)}},
            )
            return
        self._send_json(HTTPStatus.OK, {"results": results})

    def _send_hexium_package(self, path: str) -> None:
        parts = path.removeprefix("/api/hexium/package/").split("/")
        if len(parts) != 2:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_package", "message": "A package identity is required"}},
            )
            return
        namespace, name = (unquote(part) for part in parts)
        try:
            package = self.package_catalog.package(namespace, name)
        except hexium_catalog.InvalidPackage as error:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "invalid_package", "message": str(error)}},
            )
            return
        except hexium_catalog.PackageNotFound as error:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "package_not_found", "message": str(error)}},
            )
            return
        except hexium_catalog.CatalogError as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": "hexium_unavailable", "message": str(error)}},
            )
            return
        self._send_json(HTTPStatus.OK, package)

    def _send_pending_status(self, action: str) -> None:
        request_file, result_file, _builder, _audit_action = PENDING_ACTIONS[action]
        request_value = pending.read_pending_request(self.pending_directory, request_file)
        result_value = pending.read_result(self.state_directory, result_file)
        self._send_json(
            HTTPStatus.OK,
            {
                "pending": request_value is not None,
                "request": request_value,
                "result": result_value,
            },
        )

    def _handle_pending_submit(self, action: str) -> None:
        request_file, _result_file, builder, audit_action = PENDING_ACTIONS[action]
        session = self.session_store.get(parse_session_cookie(self.headers.get("Cookie")))
        try:
            require_safe_state_change(
                method="POST",
                host_header=self.headers.get("Host"),
                origin_header=self.headers.get("Origin"),
                content_type_header=self.headers.get("Content-Type"),
                csrf_header=self.headers.get("X-CSRF-Token"),
                session=session,
                allowed_host=self.allowed_host,
            )
            payload = self._read_json_body()
            normalized = builder(payload)
        except RequestRejected as error:
            self._record_audit(audit_action, "rejected", error.message)
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": {"code": error.code, "message": error.message}},
            )
            return
        except (pending.ValidationError, ValueError) as error:
            self._record_audit(audit_action, "rejected", str(error))
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "validation_failed", "message": str(error)}},
            )
            return

        with self.pending_lock:
            try:
                pending.write_pending_request(self.pending_directory, request_file, normalized)
            except FileExistsError:
                self._record_audit(audit_action, "rejected", "a request is already pending")
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": {
                            "code": "already_pending",
                            "message": "A request of this type is already pending",
                        }
                    },
                )
                return

        self._record_audit(audit_action, "accepted", "")
        self._send_json(HTTPStatus.ACCEPTED, {"request": normalized})

    def _read_json_body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length header is invalid") from error
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("request body is too large")
        raw_body = self.rfile.read(length) if length else b""
        if not raw_body:
            raise ValueError("request body must be JSON")
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("request body must be valid JSON") from error

    def _record_audit(self, action: str, outcome: str, detail: str) -> None:
        audit.record_event(
            self.audit_directory,
            action=action,
            outcome=outcome,
            detail=detail,
            remote_address=self.client_address[0] if self.client_address else None,
        )

    def _send_method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "error": {
                    "code": "method_not_allowed",
                    "message": "Only GET requests are supported",
                }
            },
            allow="GET",
        )

    def _send_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        allow: str | None = None,
        set_cookie: str | None = None,
    ) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if allow is not None:
            self.send_header("Allow", allow)
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, request_path: str) -> None:
        assert self.static_directory is not None
        if request_path == "/":
            candidate = self.static_directory / "index.html"
        else:
            decoded_path = PurePosixPath(unquote(request_path.lstrip("/")))
            if decoded_path.is_absolute() or any(
                part in {"", ".", ".."} for part in decoded_path.parts
            ):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = self.static_directory.joinpath(*decoded_path.parts)

        static_root = self.static_directory.resolve(strict=True)
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not resolved_candidate.is_relative_to(static_root) or not resolved_candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(resolved_candidate.name)[0]
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            content_type or "application/octet-stream",
        )
        self.send_header("Content-Length", str(resolved_candidate.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with resolved_candidate.open("rb") as static_file:
            shutil.copyfileobj(static_file, self.wfile)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid emitting request logs while running the development harness."""


def create_server(
    *,
    static_directory: Path | None = None,
    status_path: Path = STATUS_EXPORT_PATH,
    pending_directory: Path = PENDING_DIRECTORY,
    state_directory: Path = STATE_DIRECTORY,
    audit_directory: Path = AUDIT_DIRECTORY,
    world_backup_path: Path = WORLD_BACKUP_EXPORT_PATH,
    package_catalog: hexium_catalog.HexiumCatalog | None = None,
    allowed_host: str | None = None,
    port: int = PORT,
) -> ThreadingHTTPServer:
    """Create a localhost-only server with an optional compiled frontend."""

    if static_directory is not None and not static_directory.is_dir():
        raise ValueError(f"Static directory does not exist: {static_directory}")

    class RequestHandler(DashboardRequestHandler):
        pass

    RequestHandler.static_directory = static_directory
    RequestHandler.status_path = status_path
    RequestHandler.pending_directory = pending_directory
    RequestHandler.state_directory = state_directory
    RequestHandler.audit_directory = audit_directory
    RequestHandler.world_backup_path = world_backup_path
    RequestHandler.package_catalog = package_catalog or hexium_catalog.HexiumCatalog()
    RequestHandler.allowed_host = allowed_host or default_allowed_host()
    RequestHandler.session_store = SessionStore()
    RequestHandler.pending_lock = threading.Lock()
    server = ThreadingHTTPServer((HOST, port), RequestHandler)
    server.daemon_threads = True
    server.block_on_close = False
    return server


def create_development_server(*, port: int = 0) -> ThreadingHTTPServer:
    """Create the localhost-only health harness used during development."""

    return create_server(port=port)


def run_development_server() -> None:
    """Run the localhost-only development health endpoint."""

    server = create_development_server(port=PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_production_server(static_directory: Path) -> None:
    """Serve the production API and compiled frontend on the loopback listener."""

    server = create_server(static_directory=static_directory)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def run_configured_server() -> None:
    """Use production static assets only when the service provides their path."""

    static_directory = os.environ.get("VALHEIM_DASHBOARD_STATIC_DIR")
    if static_directory is None:
        run_development_server()
        return
    run_production_server(Path(static_directory))
