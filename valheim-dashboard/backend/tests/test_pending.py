"""Tests for fixed-schema pending job validation and directory transport."""

from pathlib import Path
import tempfile
import unittest

from src import pending


class ManifestValidationTests(unittest.TestCase):
    def test_valid_request_is_normalized(self) -> None:
        result = pending.build_manifest_request(
            {
                "packages": [
                    {"namespace": "denikson", "name": "BepInExPack_Valheim", "version": "5.4.2350"}
                ]
            }
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertIn("requested_at", result)
        self.assertEqual(
            result["packages"],
            [
                {
                    "source": "hexium",
                    "namespace": "denikson",
                    "name": "BepInExPack_Valheim",
                    "version": "5.4.2350",
                    "channel": "stable",
                    "role": "server",
                }
            ],
        )

    def test_rejects_empty_packages(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request({"packages": []})

    def test_rejects_non_list_packages(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request({"packages": "not-a-list"})

    def test_rejects_too_many_packages(self) -> None:
        packages = [
            {"namespace": "ns", "name": f"pkg{i}", "version": "1.0.0"}
            for i in range(pending.MAX_PACKAGES + 1)
        ]
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request({"packages": packages})

    def test_rejects_invalid_namespace(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {"packages": [{"namespace": "bad/ns", "name": "pkg", "version": "1.0.0"}]}
            )

    def test_rejects_invalid_version(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {"packages": [{"namespace": "ns", "name": "pkg", "version": "; rm -rf /"}]}
            )

    def test_allows_latest_version(self) -> None:
        result = pending.build_manifest_request(
            {"packages": [{"namespace": "ns", "name": "pkg", "version": "latest"}]}
        )
        self.assertEqual(result["packages"][0]["version"], "latest")

    def test_rejects_invalid_channel(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {
                    "packages": [
                        {"namespace": "ns", "name": "pkg", "version": "1.0.0", "channel": "beta"}
                    ]
                }
            )

    def test_rejects_invalid_role(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {
                    "packages": [
                        {"namespace": "ns", "name": "pkg", "version": "1.0.0", "role": "admin"}
                    ]
                }
            )

    def test_rejects_duplicate_packages(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {
                    "packages": [
                        {"namespace": "ns", "name": "pkg", "version": "1.0.0"},
                        {"namespace": "ns", "name": "pkg", "version": "2.0.0"},
                    ]
                }
            )

    def test_rejects_unsupported_field(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(
                {
                    "packages": [{"namespace": "ns", "name": "pkg", "version": "1.0.0"}],
                    "extra": True,
                }
            )

    def test_rejects_non_object_body(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_manifest_request(["not", "an", "object"])


class ConfigValidationTests(unittest.TestCase):
    def test_normalizes_a_bounded_config_update_request(self) -> None:
        result = pending.build_config_request(
            {
                "files": [
                    {
                        "path": "Author/Example.cfg",
                        "updates": [
                            {"section": "General", "key": "Enabled", "value": "false"}
                        ],
                    }
                ]
            }
        )
        self.assertEqual(result["files"][0]["path"], "Author/Example.cfg")
        self.assertEqual(result["files"][0]["updates"][0]["value"], "false")

    def test_rejects_unsafe_config_paths_and_multiline_values(self) -> None:
        with self.assertRaisesRegex(pending.ValidationError, "path is invalid"):
            pending.build_config_request(
                {
                    "files": [
                        {
                            "path": "../Example.cfg",
                            "updates": [
                                {"section": "General", "key": "Enabled", "value": "false"}
                            ],
                        }
                    ]
                }
            )
        with self.assertRaisesRegex(pending.ValidationError, "is invalid"):
            pending.build_config_request(
                {
                    "files": [
                        {
                            "path": "Example.cfg",
                            "updates": [
                                {
                                    "section": "General",
                                    "key": "Enabled",
                                    "value": "false\ntrue",
                                }
                            ],
                        }
                    ]
                }
            )


class UpdateAndRollbackValidationTests(unittest.TestCase):
    def test_update_requires_exact_confirmation(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_update_request({"confirmation": "update"})
        with self.assertRaises(pending.ValidationError):
            pending.build_update_request({"confirmation": "UPDATE", "unexpected": "field"})
        with self.assertRaises(pending.ValidationError):
            pending.build_update_request({"confirmation": "UPDATE", "reason": "x" * 201})
        result = pending.build_update_request({"confirmation": "UPDATE", "reason": "install mods"})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["reason"], "install mods")

    def test_rollback_requires_exact_confirmation(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_rollback_request({"confirmation": "rollback"})
        result = pending.build_rollback_request({"confirmation": "ROLLBACK"})
        self.assertEqual(result["schema_version"], 1)

    def test_reason_is_bounded(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_update_request(
                {"confirmation": "UPDATE", "reason": "x" * (pending.MAX_REASON_CHARACTERS + 1)}
            )

    def test_rejects_unsupported_field(self) -> None:
        with self.assertRaises(pending.ValidationError):
            pending.build_update_request({"confirmation": "UPDATE", "extra": 1})

    def test_world_restore_requires_an_opaque_backup_id_and_confirmation(self) -> None:
        backup_id = "hRz3QEji2LLaD7FpnGv5Mf0Y"
        with self.assertRaises(pending.ValidationError):
            pending.build_world_restore_request(
                {"backup_id": "../../world", "confirmation": "RESTORE"}
            )
        with self.assertRaises(pending.ValidationError):
            pending.build_world_restore_request(
                {"backup_id": backup_id, "confirmation": "restore"}
            )
        result = pending.build_world_restore_request(
            {"backup_id": backup_id, "confirmation": "RESTORE", "reason": "recover world"}
        )
        self.assertEqual(result["backup_id"], backup_id)
        self.assertEqual(result["reason"], "recover world")


class DirectoryTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_write_then_read_pending_request(self) -> None:
        request = pending.build_update_request({"confirmation": "UPDATE"})
        pending.write_pending_request(self.directory, pending.UPDATE_REQUEST_FILE, request)
        read_back = pending.read_pending_request(self.directory, pending.UPDATE_REQUEST_FILE)
        self.assertEqual(read_back, request)

    def test_update_request_uses_its_own_pending_file(self) -> None:
        request = pending.build_update_request({"confirmation": "UPDATE"})
        pending.write_pending_request(self.directory, pending.UPDATE_REQUEST_FILE, request)
        self.assertEqual(
            pending.read_pending_request(self.directory, pending.UPDATE_REQUEST_FILE),
            request,
        )

    def test_write_refuses_to_overwrite_pending_request(self) -> None:
        request = pending.build_update_request({"confirmation": "UPDATE"})
        pending.write_pending_request(self.directory, pending.UPDATE_REQUEST_FILE, request)
        with self.assertRaises(FileExistsError):
            pending.write_pending_request(self.directory, pending.UPDATE_REQUEST_FILE, request)

    def test_read_pending_request_missing_file_returns_none(self) -> None:
        self.assertIsNone(pending.read_pending_request(self.directory, pending.UPDATE_REQUEST_FILE))

    def test_read_result_missing_file_returns_none(self) -> None:
        self.assertIsNone(pending.read_result(self.directory, pending.UPDATE_RESULT_FILE))

    def test_read_result_returns_bounded_result(self) -> None:
        result_path = self.directory / pending.UPDATE_RESULT_FILE
        result_path.write_text('{"outcome": "success"}', encoding="utf-8")
        self.assertEqual(pending.read_result(self.directory, pending.UPDATE_RESULT_FILE), {"outcome": "success"})

    def test_read_result_rejects_non_object_json(self) -> None:
        result_path = self.directory / pending.UPDATE_RESULT_FILE
        result_path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(pending.read_result(self.directory, pending.UPDATE_RESULT_FILE))

    def test_read_result_rejects_oversized_file(self) -> None:
        result_path = self.directory / pending.UPDATE_RESULT_FILE
        result_path.write_text("{" + '"a":"' + "x" * pending.MAX_RESULT_BYTES + '"}', encoding="utf-8")
        self.assertIsNone(pending.read_result(self.directory, pending.UPDATE_RESULT_FILE))


if __name__ == "__main__":
    unittest.main()
