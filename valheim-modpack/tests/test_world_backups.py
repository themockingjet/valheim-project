"""Focused scratch-directory tests for world backup inventory and restore."""

import json
from pathlib import Path
import tempfile
import unittest

from src import world_backups


class WorldBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.worlds = self.root / "worlds_local"
        self.worlds.mkdir()
        self.index = self.root / "state" / "backup-index.json"
        self.export = self.root / "exports" / "world-backups.json"
        self.transactions = self.root / ".dashboard-world-restores"
        self.active = self.write_snapshot("Example World", "active")
        self.backup = self.write_snapshot(
            "Example World_backup_auto-20260913-080632", "backup"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_snapshot(self, name: str, chunk_contents: str) -> Path:
        snapshot = self.worlds / name
        snapshot.mkdir()
        (snapshot / "_main.1.db2").write_text("database", encoding="utf-8")
        (snapshot / "_main.1.fwl2").write_text("world", encoding="utf-8")
        (snapshot / "_main.1.ok").write_text("ok", encoding="utf-8")
        (snapshot / "00_00__0_1.chunk").write_text(chunk_contents, encoding="utf-8")
        return snapshot

    def publish(self) -> dict[str, object]:
        return world_backups.publish_inventory(
            worlds_directory=self.worlds,
            index_path=self.index,
            output_path=self.export,
        )

    def test_inventory_uses_stable_opaque_ids_and_never_exports_paths(self) -> None:
        first = self.publish()
        second = self.publish()
        first_backup = first["backups"][0]
        self.assertEqual(first_backup["id"], second["backups"][0]["id"])
        self.assertEqual(first["native_retention"]["observed_count"], 1)
        rendered = self.export.read_text(encoding="utf-8")
        self.assertNotIn(str(self.worlds), rendered)
        self.assertNotIn(self.backup.name, rendered)
        self.assertEqual(first["active_world"]["integrity"], "ready")

    def test_inventory_reports_unavailable_before_the_first_world_exists(self) -> None:
        self.active.rename(self.root / "active-away")
        self.backup.rename(self.root / "backup-away")

        inventory = self.publish()

        self.assertEqual(
            inventory["active_world"],
            {"file_count": 0, "size_bytes": 0, "integrity": "unavailable"},
        )
        self.assertEqual(inventory["native_retention"]["observed_count"], 0)
        self.assertEqual(inventory["backups"], [])

    def test_inventory_reports_unavailable_before_worlds_directory_exists(self) -> None:
        self.active.rename(self.root / "active-away")
        self.backup.rename(self.root / "backup-away")
        self.worlds.rmdir()

        inventory = self.publish()

        self.assertEqual(inventory["active_world"]["integrity"], "unavailable")
        self.assertEqual(inventory["backups"], [])

    def test_incomplete_or_symlinked_backup_is_rejected(self) -> None:
        (self.backup / "_main.1.ok").unlink()
        with self.assertRaises(world_backups.WorldBackupError):
            self.publish()

        (self.backup / "_main.1.ok").write_text("ok", encoding="utf-8")
        (self.backup / "unsafe").symlink_to(self.active)
        with self.assertRaises(world_backups.WorldBackupError):
            self.publish()

    def test_incomplete_active_world_publishes_unavailable_inventory(self) -> None:
        (self.active / "_main.1.ok").unlink()

        inventory = self.publish()

        self.assertEqual(inventory["active_world"]["integrity"], "unavailable")
        self.assertEqual(inventory["native_retention"]["observed_count"], 0)
        self.assertEqual(inventory["backups"], [])

    def test_prepare_and_complete_restore_whole_directory(self) -> None:
        inventory = self.publish()
        backup_id = inventory["backups"][0]["id"]
        transaction_id = world_backups.prepare_restore(
            backup_id,
            worlds_directory=self.worlds,
            index_path=self.index,
            transaction_directory=self.transactions,
        )
        self.assertEqual(
            (self.worlds / "Example World" / "00_00__0_1.chunk").read_text(),
            "backup",
        )
        transaction = self.transactions / transaction_id
        self.assertTrue((transaction / "previous-active").is_dir())
        record = json.loads((transaction / "transaction.json").read_text())
        self.assertEqual(record["phase"], "restored")

        world_backups.complete_restore(transaction_id, transaction_directory=self.transactions)
        record = json.loads((transaction / "transaction.json").read_text())
        self.assertEqual(record["phase"], "completed")

    def test_failed_restore_rolls_back_to_pre_restore_world(self) -> None:
        backup_id = self.publish()["backups"][0]["id"]
        transaction_id = world_backups.prepare_restore(
            backup_id,
            worlds_directory=self.worlds,
            index_path=self.index,
            transaction_directory=self.transactions,
        )
        world_backups.rollback_restore(
            transaction_id,
            worlds_directory=self.worlds,
            transaction_directory=self.transactions,
        )
        self.assertEqual(
            (self.worlds / "Example World" / "00_00__0_1.chunk").read_text(),
            "active",
        )
        record = json.loads(
            (self.transactions / transaction_id / "transaction.json").read_text()
        )
        self.assertEqual(record["phase"], "recovered")

    def test_recovery_reverts_an_unconfirmed_restore(self) -> None:
        backup_id = self.publish()["backups"][0]["id"]
        transaction_id = world_backups.prepare_restore(
            backup_id,
            worlds_directory=self.worlds,
            index_path=self.index,
            transaction_directory=self.transactions,
        )
        recovered = world_backups.recover_incomplete_restores(
            worlds_directory=self.worlds,
            transaction_directory=self.transactions,
        )
        self.assertEqual(recovered, [transaction_id])
        self.assertEqual(
            (self.worlds / "Example World" / "00_00__0_1.chunk").read_text(),
            "active",
        )

    def test_unknown_backup_id_cannot_select_a_directory(self) -> None:
        self.publish()
        with self.assertRaises(world_backups.WorldBackupError):
            world_backups.prepare_restore(
                "../../Example World",
                worlds_directory=self.worlds,
                index_path=self.index,
                transaction_directory=self.transactions,
            )


if __name__ == "__main__":
    unittest.main()
