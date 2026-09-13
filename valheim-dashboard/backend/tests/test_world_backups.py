"""Tests for root-exported world-backup inventory validation."""

import json
from pathlib import Path
import tempfile
import unittest

from src import world_backups


def valid_inventory() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-13T00:00:00Z",
        "active_world": {"file_count": 18, "size_bytes": 6125744, "integrity": "ready"},
        "native_retention": {"observed_count": 4},
        "backups": [
            {
                "id": "hRz3QEji2LLaD7FpnGv5Mf0Y",
                "kind": "native-auto",
                "created_at": "2026-09-13T00:06:32Z",
                "size_bytes": 6125744,
                "file_count": 18,
                "integrity": "ready",
            }
        ],
    }


class WorldBackupInventoryTests(unittest.TestCase):
    def test_loads_a_valid_bounded_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world-backups.json"
            expected = valid_inventory()
            path.write_text(json.dumps(expected), encoding="utf-8")
            self.assertEqual(world_backups.load_inventory(path), expected)

    def test_rejects_paths_and_unknown_fields(self) -> None:
        inventory = valid_inventory()
        inventory["backups"][0]["path"] = "/opt/valheim/data/worlds_local/secret"
        with self.assertRaises(world_backups.WorldBackupError):
            world_backups.validate_inventory(inventory)

    def test_rejects_non_opaque_backup_ids(self) -> None:
        inventory = valid_inventory()
        inventory["backups"][0]["id"] = "../../world"
        with self.assertRaises(world_backups.WorldBackupError):
            world_backups.validate_inventory(inventory)

    def test_rejects_oversized_or_missing_inventories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world-backups.json"
            with self.assertRaises(world_backups.WorldBackupUnavailable):
                world_backups.load_inventory(path)
            path.write_bytes(b" " * (world_backups.MAX_INVENTORY_BYTES + 1))
            with self.assertRaises(world_backups.WorldBackupError):
                world_backups.load_inventory(path)


if __name__ == "__main__":
    unittest.main()
