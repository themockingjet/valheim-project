"""Tests for validation of the root-exported BepInEx configuration view."""

import json
from pathlib import Path
import tempfile
import unittest

from src.mod_configs import ConfigSnapshotError, load_config_export, validate_request_targets


def document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "files": [
            {
                "path": "Example.cfg",
                "entries": [{"section": "General", "key": "Enabled", "value": "true"}],
            }
        ],
        "errors": [],
    }


class ModConfigSnapshotTests(unittest.TestCase):
    def test_loads_a_valid_export_and_rejects_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mod-configs.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            self.assertEqual(load_config_export(path), document())

            invalid = document()
            invalid["files"][0]["entries"].append(
                {"section": "General", "key": "Enabled", "value": "false"}
            )
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ConfigSnapshotError, "duplicate entries"):
                load_config_export(path)

    def test_accepts_known_request_targets_only(self) -> None:
        known = {
            "schema_version": 1,
            "requested_at": "2026-09-13T05:00:00+00:00",
            "files": [
                {
                    "path": "Example.cfg",
                    "updates": [{"section": "General", "key": "Enabled", "value": "false"}],
                }
            ],
        }
        validate_request_targets(known, document())
        known["files"][0]["updates"][0]["key"] = "Missing"
        with self.assertRaisesRegex(ConfigSnapshotError, "unknown file or key"):
            validate_request_targets(known, document())


if __name__ == "__main__":
    unittest.main()
