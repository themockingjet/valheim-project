"""Tests for the development dashboard backend harness."""

import http.client
import json
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from src import audit, pending
from src.hexium_catalog import InvalidPackage, InvalidSearch, PackageNotFound
from src.app import HOST, create_development_server, create_server
from src.status_snapshot import MAX_SNAPSHOT_BYTES


class _FakePackageCatalog:
    def search(self, query: str) -> list[dict[str, object]]:
        if query == "../../etc":
            raise InvalidSearch("Search must be 2-64 letters, numbers, spaces, dots, or dashes")
        if query != "wards":
            return []
        return [
            {
                "namespace": "ExampleAuthor",
                "name": "BetterWards",
                "latest_version": "1.2.3",
                "description": "Example package",
                "dependency_count": 1,
            }
        ]

    def package(self, namespace: str, name: str) -> dict[str, object]:
        if namespace == "../../etc":
            raise InvalidPackage("Package identity is invalid")
        if namespace != "ExampleAuthor" or name != "BetterWards":
            raise PackageNotFound("Package was not found in the Hexium catalogue")
        return {
            "namespace": namespace,
            "name": name,
            "latest_version": "1.2.3",
            "versions": ["1.2.3", "1.2.2"],
        }


class DashboardHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_development_server()
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def test_health_endpoint_is_localhost_only(self) -> None:
        self.assertEqual(self.server.server_address[0], HOST)

    def test_health_endpoint_returns_ok_json(self) -> None:
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1]
        )
        connection.request("GET", "/healthz")
        response = connection.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertEqual(response.read(), b'{"status":"ok"}\n')

    def test_unknown_path_returns_not_found(self) -> None:
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1]
        )
        connection.request("GET", "/")
        response = connection.getresponse()

        self.assertEqual(response.status, 404)


class ProductionServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.static_directory = Path(self.temporary_directory.name)
        (self.static_directory / "assets").mkdir()
        (self.static_directory / "index.html").write_text(
            "<!doctype html><title>Valheim Dashboard</title>",
            encoding="utf-8",
        )
        (self.static_directory / "assets" / "app.js").write_text(
            "console.log('dashboard')",
            encoding="utf-8",
        )
        self.status_path = self.static_directory / "status.json"
        self.server = create_server(
            static_directory=self.static_directory,
            status_path=self.status_path,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary_directory.cleanup()

    def request(self, path: str, method: str = "GET") -> http.client.HTTPResponse:
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1]
        )
        connection.request(method, path)
        return connection.getresponse()

    def write_valid_snapshot(self) -> dict[str, object]:
        snapshot = {
            "schema_version": 1,
            "generated_at": "2026-09-13T05:12:00+00:00",
            "server": {
                "active_state": "active",
                "sub_state": "running",
                "pid": 123,
                "uptime_seconds": 60.25,
                "memory_current_bytes": 1024,
                "cpu_usage_ns": 2000,
            },
            "maintenance": {
                "next_scheduled_at": "2026-09-13T12:00:00+08:00",
                "last": {
                    "outcome": "success",
                    "started_at": "2026-09-13T05:10:00+00:00",
                    "completed_at": "2026-09-13T05:11:00+00:00",
                    "rollback_performed": False,
                    "failure_reason": None,
                },
            },
            "modpack": {
                "active_release": "release-current",
                "previous_release": "release-previous",
                "packages": [
                    {
                        "namespace": "denikson",
                        "name": "BepInExPack_Valheim",
                        "version": "5.4.2350",
                        "role": "server",
                    }
                ],
                "manifest_packages": [
                    {
                        "namespace": "denikson",
                        "name": "BepInExPack_Valheim",
                        "version": "5.4.2350",
                        "channel": "stable",
                        "role": "server",
                    }
                ],
            },
            "logs": {"server": ["Game server connected"], "maintenance": []},
        }
        self.status_path.write_text(json.dumps(snapshot), encoding="utf-8")
        return snapshot

    def test_production_server_serves_frontend_and_api_health(self) -> None:
        index = self.request("/")
        self.assertEqual(index.status, 200)
        self.assertEqual(index.read(), b"<!doctype html><title>Valheim Dashboard</title>")

        asset = self.request("/assets/app.js")
        self.assertEqual(asset.status, 200)
        self.assertEqual(asset.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(asset.read(), b"console.log('dashboard')")

        health = self.request("/api/healthz")
        self.assertEqual(health.status, 200)
        self.assertEqual(health.read(), b'{"status":"ok"}\n')

    def test_production_server_rejects_missing_and_traversal_paths(self) -> None:
        self.assertEqual(self.request("/missing.js").status, 404)
        self.assertEqual(self.request("/%2e%2e/pyproject.toml").status, 404)

    def test_status_returns_unavailable_without_an_export(self) -> None:
        response = self.request("/api/status")

        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.read(),
            b'{"error":{"code":"snapshot_unavailable","message":"status snapshot is not available yet"}}\n',
        )

    def test_status_returns_validated_snapshot(self) -> None:
        snapshot = self.write_valid_snapshot()

        response = self.request("/api/status")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(json.loads(response.read()), snapshot)

    def test_status_events_open_a_no_store_stream(self) -> None:
        self.write_valid_snapshot()
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1]
        )
        connection.request("GET", "/api/status/events")
        response = connection.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.fp.readline(), b"event: connected\n")
        self.assertEqual(response.fp.readline(), b"data: {}\n")
        self.assertEqual(response.fp.readline(), b"\n")
        connection.close()

    def test_status_events_signal_snapshot_replacement(self) -> None:
        snapshot = self.write_valid_snapshot()
        connection = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1], timeout=1
        )
        with patch("src.app.STATUS_EVENT_POLL_SECONDS", 0.01):
            connection.request("GET", "/api/status/events")
            response = connection.getresponse()
            response.fp.readline()
            response.fp.readline()
            response.fp.readline()

            snapshot["server"]["pid"] = 456
            self.status_path.write_text(json.dumps(snapshot), encoding="utf-8")

            self.assertEqual(response.fp.readline(), b"event: snapshot\n")
            self.assertEqual(response.fp.readline(), b"data: {}\n")
            self.assertEqual(response.fp.readline(), b"\n")
        connection.close()

    def test_status_subroutes_return_only_the_requested_validated_section(self) -> None:
        snapshot = self.write_valid_snapshot()

        for path, section in (
            ("/api/server-status", "server"),
            ("/api/maintenance-status", "maintenance"),
            ("/api/modpack-status", "modpack"),
        ):
            response = self.request(path)

            self.assertEqual(response.status, 200)
            self.assertEqual(
                json.loads(response.read()),
                {
                    "schema_version": snapshot["schema_version"],
                    "generated_at": snapshot["generated_at"],
                    section: snapshot[section],
                },
            )

    def test_status_rejects_invalid_snapshot(self) -> None:
        self.status_path.write_text('{"schema_version": 2}', encoding="utf-8")

        response = self.request("/api/status")

        self.assertEqual(response.status, 503)
        self.assertEqual(
            json.loads(response.read())["error"]["code"],
            "snapshot_invalid",
        )

    def test_status_rejects_oversized_snapshot(self) -> None:
        self.status_path.write_bytes(b" " * (MAX_SNAPSHOT_BYTES + 1))

        response = self.request("/api/status")

        self.assertEqual(response.status, 503)
        self.assertEqual(
            json.loads(response.read())["error"]["code"],
            "snapshot_invalid",
        )

    def test_status_rejects_invalid_maintenance_timestamp(self) -> None:
        snapshot = self.write_valid_snapshot()
        snapshot["maintenance"]["next_scheduled_at"] = "tomorrow at noon"
        self.status_path.write_text(json.dumps(snapshot), encoding="utf-8")

        response = self.request("/api/status")

        self.assertEqual(response.status, 503)
        self.assertEqual(
            json.loads(response.read())["error"]["code"],
            "snapshot_invalid",
        )

    def test_status_rejects_invalid_utf8_snapshot(self) -> None:
        self.status_path.write_bytes(b"\xff")

        response = self.request("/api/status")

        self.assertEqual(response.status, 503)
        self.assertEqual(
            json.loads(response.read())["error"]["code"],
            "snapshot_invalid",
        )

    def test_unknown_api_route_returns_json_not_found(self) -> None:
        response = self.request("/api/not-a-route")

        self.assertEqual(response.status, 404)
        self.assertEqual(
            json.loads(response.read()),
            {"error": {"code": "not_found", "message": "API route was not found"}},
        )

    def test_unsupported_api_method_returns_json_method_not_allowed(self) -> None:
        response = self.request("/api/status", method="POST")

        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "GET")
        self.assertEqual(
            json.loads(response.read()),
            {
                "error": {
                    "code": "method_not_allowed",
                    "message": "Only GET requests are supported",
                }
            },
        )


class PendingWorkflowTests(unittest.TestCase):
    """End-to-end coverage of B2/B3/B4/B5/B6 request submission via HTTP."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.pending_directory = root / "pending"
        self.state_directory = root / "state"
        self.audit_directory = root / "audit"
        self.world_backup_path = root / "world-backups.json"
        self.mod_config_path = root / "mod-configs.json"
        self.package_catalog = _FakePackageCatalog()
        self.allowed_host = "127.0.0.1:8080"
        self.server = create_server(
            pending_directory=self.pending_directory,
            state_directory=self.state_directory,
            audit_directory=self.audit_directory,
            world_backup_path=self.world_backup_path,
            mod_config_path=self.mod_config_path,
            package_catalog=self.package_catalog,
            allowed_host=self.allowed_host,
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary_directory.cleanup()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1]
        )

    def _get_session(self):
        connection = self._connection()
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        body = json.loads(response.read())
        set_cookie = response.getheader("Set-Cookie")
        cookie = set_cookie.split(";", 1)[0]
        return cookie, body["csrf_token"]

    def _write_config_export(self) -> None:
        self.mod_config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "path": "Example.cfg",
                            "entries": [
                                {
                                    "section": "General",
                                    "key": "Enabled",
                                    "value": "true",
                                }
                            ],
                        }
                    ],
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )

    def _post(self, path, payload, *, cookie=None, csrf=None, host=None, origin=None, content_type="application/json"):
        connection = self._connection()
        headers = {}
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        if content_type is not None:
            headers["Content-Type"] = content_type
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        body = json.dumps(payload).encode("utf-8")
        connection.request("POST", path, body=body, headers=headers)
        return connection.getresponse()

    def test_session_endpoint_issues_cookie_and_token(self) -> None:
        cookie, csrf = self._get_session()
        self.assertTrue(cookie.startswith("vd_session="))
        self.assertTrue(csrf)

    def test_valid_update_request_is_accepted_and_audited(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE", "reason": "install queued packages"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 202)
        request_on_disk = pending.read_pending_request(
            self.pending_directory, pending.UPDATE_REQUEST_FILE
        )
        self.assertIsNotNone(request_on_disk)
        self.assertEqual(request_on_disk["reason"], "install queued packages")
        self.assertEqual(
            audit.read_recent_events(self.audit_directory)[-1]["action"],
            "update_request",
        )
        connection = self._connection()
        connection.request("GET", "/api/pending/update")
        status_response = connection.getresponse()
        status_payload = json.loads(status_response.read())
        self.assertTrue(status_payload["pending"])
        self.assertEqual(status_payload["request"]["reason"], "install queued packages")

    def test_restart_only_request_is_not_available(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/restart",
            {"confirmation": "RESTART"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 405)
        self.assertFalse((self.pending_directory / "restart-request.json").exists())

    def test_duplicate_update_request_is_rejected_as_conflict(self) -> None:
        cookie, csrf = self._get_session()
        self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 409)

    def test_missing_csrf_token_is_rejected(self) -> None:
        cookie, _csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=None,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "csrf_rejected")

    def test_mismatched_origin_is_rejected(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin="http://evil.example",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "origin_rejected")

    def test_mismatched_host_is_rejected(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host="evil.example",
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "host_rejected")

    def test_non_json_content_type_is_rejected(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
            content_type="text/plain",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "content_type_rejected")

    def test_no_session_is_rejected(self) -> None:
        response = self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "session_required")

    def test_invalid_confirmation_is_rejected_and_audited(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/update",
            {"confirmation": "please"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 400)
        events = audit.read_recent_events(self.audit_directory)
        self.assertEqual(events[-1]["outcome"], "rejected")

    def test_manifest_submission_round_trips_through_pending_status(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/manifest",
            {
                "packages": [
                    {"namespace": "denikson", "name": "BepInExPack_Valheim", "version": "5.4.2350"}
                ]
            },
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 202)

        connection = self._connection()
        connection.request("GET", "/api/pending/manifest")
        status_response = connection.getresponse()
        status_payload = json.loads(status_response.read())
        self.assertTrue(status_payload["pending"])
        self.assertEqual(len(status_payload["request"]["packages"]), 1)
        self.assertIsNone(status_payload["result"])

    def test_config_submission_uses_exported_file_and_key_allow_list(self) -> None:
        self._write_config_export()
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/config",
            {
                "files": [
                    {
                        "path": "Example.cfg",
                        "updates": [
                            {
                                "section": "General",
                                "key": "Enabled",
                                "value": "false",
                            }
                        ],
                    }
                ]
            },
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 202)
        request = pending.read_pending_request(self.pending_directory, pending.CONFIG_REQUEST_FILE)
        self.assertEqual(request["files"][0]["updates"][0]["value"], "false")
        self.assertEqual(audit.read_recent_events(self.audit_directory)[-1]["action"], "config_submit")

        connection = self._connection()
        connection.request("GET", "/api/mod-configs")
        config_response = connection.getresponse()
        self.assertEqual(config_response.status, 200)
        self.assertEqual(json.loads(config_response.read())["files"][0]["path"], "Example.cfg")

    def test_config_submission_rejects_unknown_exported_key(self) -> None:
        self._write_config_export()
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/config",
            {
                "files": [
                    {
                        "path": "Example.cfg",
                        "updates": [
                            {
                                "section": "General",
                                "key": "NotEnabled",
                                "value": "false",
                            }
                        ],
                    }
                ]
            },
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 400)
        self.assertFalse((self.pending_directory / pending.CONFIG_REQUEST_FILE).exists())

    def test_rollback_requires_exact_confirmation_text(self) -> None:
        cookie, csrf = self._get_session()
        response = self._post(
            "/api/pending/rollback",
            {"confirmation": "ROLLBACK"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 202)

    def test_world_restore_requires_confirmation_and_records_opaque_job(self) -> None:
        cookie, csrf = self._get_session()
        backup_id = "hRz3QEji2LLaD7FpnGv5Mf0Y"
        response = self._post(
            "/api/pending/world-restore",
            {"backup_id": backup_id, "confirmation": "RESTORE", "reason": "test restore"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        self.assertEqual(response.status, 202)
        request = pending.read_pending_request(
            self.pending_directory, pending.WORLD_RESTORE_REQUEST_FILE
        )
        self.assertEqual(request["backup_id"], backup_id)
        self.assertNotIn("confirmation", request)
        self.assertEqual(
            audit.read_recent_events(self.audit_directory)[-1]["action"],
            "world_restore_request",
        )

    def test_world_backups_endpoint_validates_root_export(self) -> None:
        self.world_backup_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-09-13T00:00:00Z",
                    "active_world": {
                        "file_count": 18,
                        "size_bytes": 6125744,
                        "integrity": "ready",
                    },
                    "native_retention": {"observed_count": 4},
                    "backups": [],
                }
            ),
            encoding="utf-8",
        )
        connection = self._connection()
        connection.request("GET", "/api/world-backups")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["backups"], [])

    def test_hexium_search_returns_matches_from_the_fixed_catalogue(self) -> None:
        connection = self._connection()
        connection.request("GET", "/api/hexium/search?q=wards")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.read())["results"],
            [
                {
                    "namespace": "ExampleAuthor",
                    "name": "BetterWards",
                    "latest_version": "1.2.3",
                    "description": "Example package",
                    "dependency_count": 1,
                }
            ],
        )

    def test_hexium_search_rejects_missing_or_repeated_query(self) -> None:
        for path in ("/api/hexium/search", "/api/hexium/search?q=ok&q=again"):
            connection = self._connection()
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 400)

    def test_hexium_search_rejects_an_unsafe_query_as_bad_request(self) -> None:
        connection = self._connection()
        connection.request("GET", "/api/hexium/search?q=../../etc")
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.read())["error"]["code"], "invalid_search")

    def test_hexium_package_returns_available_versions(self) -> None:
        connection = self._connection()
        connection.request("GET", "/api/hexium/package/ExampleAuthor/BetterWards")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(response.read()),
            {
                "namespace": "ExampleAuthor",
                "name": "BetterWards",
                "latest_version": "1.2.3",
                "versions": ["1.2.3", "1.2.2"],
            },
        )

    def test_hexium_package_rejects_unknown_package(self) -> None:
        connection = self._connection()
        connection.request("GET", "/api/hexium/package/Unknown/Missing")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(response.read())["error"]["code"], "package_not_found")

    def test_audit_endpoint_reports_bounded_events(self) -> None:
        cookie, csrf = self._get_session()
        self._post(
            "/api/pending/update",
            {"confirmation": "UPDATE"},
            cookie=cookie,
            csrf=csrf,
            host=self.allowed_host,
            origin=f"http://{self.allowed_host}",
        )
        connection = self._connection()
        connection.request("GET", "/api/audit")
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(payload["events"][-1]["action"], "update_request")

    def test_oversized_body_is_rejected(self) -> None:
        cookie, csrf = self._get_session()
        connection = self._connection()
        big_body = json.dumps({"confirmation": "UPDATE", "reason": "x" * 200000}).encode("utf-8")
        connection.request(
            "POST",
            "/api/pending/update",
            body=big_body,
            headers={
                "Host": self.allowed_host,
                "Origin": f"http://{self.allowed_host}",
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
            },
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
