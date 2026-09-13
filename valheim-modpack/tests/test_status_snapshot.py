"""Focused tests for the bounded status snapshot exporter."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from src import status_snapshot


class Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class StatusSnapshotTests(unittest.TestCase):
    def make_release(
        self, root: Path, name: str, package_name: str = "Author-Mod-1.2.3"
    ) -> None:
        release = root / "releases" / name
        release.mkdir(parents=True)
        (release / "release-lock.json").write_text(
            json.dumps({"schema_version": 1, "packages": [package_name]}),
            encoding="utf-8",
        )

    def test_builds_contract_without_sensitive_lock_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "releases").mkdir()
            self.make_release(root, "release-current")
            self.make_release(root, "release-previous")
            (root / "current").symlink_to(root / "releases/release-current")
            (root / "previous").symlink_to(root / "releases/release-previous")
            (root / "modpack.lock.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "hexium",
                        "packages": [
                            {
                                "namespace": "Author",
                                "name": "Mod",
                                "version": "1.2.3",
                                "full_name": "Author-Mod-1.2.3",
                                "download_url": "https://example.invalid/mod.zip",
                                "sha256": "a" * 64,
                                "role": "server",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            server_log = root / "server.log"
            server_log.write_text(
                "token=do-not-leak https://example.invalid " + "x" * 600,
                encoding="utf-8",
            )
            maintenance_log = root / "maintenance.log"
            maintenance_log.write_text("maintenance started\n", encoding="utf-8")

            with patch.object(
                status_snapshot,
                "_run_fixed",
                side_effect=[
                    Completed(
                        "ActiveState=active\nSubState=running\nMainPID=123\n"
                        "ActiveEnterTimestampMonotonic=1000000\nMemoryCurrent=1024\n"
                        "CPUUsageNSec=2000\n"
                    ),
                    Completed("NextElapseUSecRealtime=@1789272000\n"),
                ],
            ):
                snapshot = status_snapshot.build_snapshot(
                    modpack_root=root,
                    server_log=server_log,
                    maintenance_log=maintenance_log,
                    maintenance_state=root / "missing-state.json",
                    generated_at="2026-09-13T00:00:00Z",
                )

            self.assertEqual(snapshot["schema_version"], 1)
            self.assertEqual(snapshot["server"]["pid"], 123)
            self.assertEqual(
                snapshot["maintenance"]["next_scheduled_at"],
                "2026-09-13T04:00:00Z",
            )
            self.assertEqual(snapshot["modpack"]["active_release"], "release-current")
            self.assertEqual(snapshot["modpack"]["packages"][0]["role"], "server")
            self.assertNotIn("download_url", json.dumps(snapshot))
            self.assertNotIn("sha256", json.dumps(snapshot))
            self.assertLessEqual(len(snapshot["logs"]["server"][0]), 500)
            self.assertIn("[redacted-url]", snapshot["logs"]["server"][0])
            self.assertIn("[redacted]", snapshot["logs"]["server"][0])

    def test_publish_replaces_atomically_with_bounded_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "exports/status.json"
            snapshot = {
                "schema_version": 1,
                "generated_at": "2026-09-13T00:00:00Z",
                "server": {},
                "maintenance": {},
                "modpack": {},
                "logs": {"server": [], "maintenance": []},
            }
            status_snapshot.publish_snapshot(snapshot, output)

            self.assertEqual(json.loads(output.read_text()), snapshot)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)
            self.assertFalse(list(output.parent.glob("*.new")))

    def test_maintenance_state_transitions_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "maintenance-state.json"
            running = status_snapshot._update_maintenance_state(state_path, "running")
            self.assertEqual(running["outcome"], "running")
            self.assertIsNotNone(running["started_at"])
            completed = status_snapshot._update_maintenance_state(
                state_path,
                "rolled_back",
                failure_reason="failure " + "x" * 600,
                rollback_performed=True,
            )
            self.assertEqual(completed["outcome"], "rolled_back")
            self.assertTrue(completed["rollback_performed"])
            self.assertLessEqual(len(completed["failure_reason"]), 500)

    def test_journal_and_systemctl_commands_are_fixed_and_bounded(self) -> None:
        with patch.object(
            status_snapshot,
            "_run_fixed",
            return_value=Completed("\n".join(f"entry-{index}" for index in range(200))),
        ) as run:
            lines = status_snapshot._command_lines(status_snapshot.JOURNAL_ARGS)

        run.assert_called_once_with(status_snapshot.JOURNAL_ARGS)
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[0], "entry-100")


if __name__ == "__main__":
    with redirect_stdout(io.StringIO()):
        unittest.main()
