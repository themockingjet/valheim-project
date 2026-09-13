"""Tests for safe BepInEx configuration exports and queued overrides."""

import json
from pathlib import Path
import tempfile
import unittest

from src.config_overrides import (
    ConfigError,
    apply_config_request,
    build_config_export,
    publish_config_export,
)


def request(path: str, updates: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "requested_at": "2026-09-13T05:00:00+00:00",
        "files": [{"path": path, "updates": updates}],
    }


class ConfigOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.release = self.root / "releases" / "release-one"
        (self.release / "BepInEx" / "config").mkdir(parents=True)
        (self.release / "release-lock.json").write_text("{}", encoding="utf-8")
        (self.root / "current").symlink_to(self.release)
        self.config = self.release / "BepInEx" / "config" / "Example.cfg"
        self.config.write_text(
            "# retained comment\n[General]\nEnabled = true\nUnknown setting\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_exports_active_config_and_prefers_existing_override(self) -> None:
        overrides = self.root / "config-overrides"
        overrides.mkdir()
        (overrides / "Example.cfg").write_text(
            "[General]\nEnabled = false\n", encoding="utf-8"
        )

        export = build_config_export(self.root)

        self.assertEqual(
            export["files"],
            [
                {
                    "path": "Example.cfg",
                    "entries": [{"section": "General", "key": "Enabled", "value": "false"}],
                }
            ],
        )

    def test_applies_value_updates_without_losing_comments_or_unknown_lines(self) -> None:
        updated = apply_config_request(
            request(
                "Example.cfg",
                [{"section": "General", "key": "Enabled", "value": "false"}],
            ),
            modpack_root=self.root,
        )

        self.assertEqual(updated, [{"path": "Example.cfg", "updated_keys": 1}])
        override = self.root / "config-overrides" / "Example.cfg"
        self.assertEqual(
            override.read_text(encoding="utf-8"),
            "# retained comment\n[General]\nEnabled = false\nUnknown setting\n",
        )
        self.assertEqual(self.config.read_text(encoding="utf-8").splitlines()[2], "Enabled = true")

    def test_rejects_unknown_keys_and_unsafe_paths_without_writing(self) -> None:
        with self.assertRaisesRegex(ConfigError, "not managed"):
            apply_config_request(
                request(
                    "Example.cfg",
                    [{"section": "General", "key": "Missing", "value": "false"}],
                ),
                modpack_root=self.root,
            )
        with self.assertRaisesRegex(ConfigError, "path is invalid"):
            apply_config_request(
                request(
                    "../outside.cfg",
                    [{"section": "General", "key": "Enabled", "value": "false"}],
                ),
                modpack_root=self.root,
            )
        self.assertFalse((self.root / "config-overrides").exists())

    def test_validated_batch_does_not_partially_write_when_a_later_file_is_invalid(self) -> None:
        second = self.release / "BepInEx" / "config" / "Second.cfg"
        second.write_text("[General]\nEnabled = true\n", encoding="utf-8")
        batch = request(
            "Example.cfg",
            [{"section": "General", "key": "Enabled", "value": "false"}],
        )
        batch["files"].append(
            {
                "path": "Second.cfg",
                "updates": [{"section": "General", "key": "Missing", "value": "false"}],
            }
        )

        with self.assertRaisesRegex(ConfigError, "not managed"):
            apply_config_request(batch, modpack_root=self.root)

        self.assertFalse((self.root / "config-overrides" / "Example.cfg").exists())

    def test_skips_symlinked_config_files_and_publishes_bounded_json(self) -> None:
        external = self.root / "outside.cfg"
        external.write_text("[General]\nEnabled = false\n", encoding="utf-8")
        (self.release / "BepInEx" / "config" / "Linked.cfg").symlink_to(external)
        output = self.root / "mod-configs.json"

        publish_config_export(self.root, output)

        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([file["path"] for file in document["files"]], ["Example.cfg"])
        self.assertFalse((self.root / "config-overrides").exists())

    def test_reports_invalid_duplicate_entries_without_hiding_valid_files(self) -> None:
        second = self.release / "BepInEx" / "config" / "Duplicate.cfg"
        second.write_text("[General]\nEnabled = true\nEnabled = false\n", encoding="utf-8")

        export = build_config_export(self.root)

        self.assertEqual([file["path"] for file in export["files"]], ["Example.cfg"])
        self.assertEqual(export["errors"][0]["path"], "Duplicate.cfg")


if __name__ == "__main__":
    unittest.main()
