"""Static checks for dashboard helper safety and deploy coverage."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
SCRIPTS = PROJECT_ROOT / "scripts" / "modpack"
SYSTEMD = PROJECT_ROOT / "systemd" / "modpack"
SERVER_SCRIPTS = PROJECT_ROOT / "scripts" / "server"
SERVER_SYSTEMD = PROJECT_ROOT / "systemd" / "server"


class DashboardHelperDeploymentTests(unittest.TestCase):
    def test_shell_scripts_parse_and_deployer_has_safe_enable_gate(self) -> None:
        scripts = (
            "valheim-manifest-apply",
            "valheim-restart-request",
            "valheim-rollback-request",
            "valheim-world-restore",
            "valheim-dashboard-actions",
            "valheim-modpack-deploy",
        )
        for name in scripts:
            with self.subTest(name=name):
                subprocess.run(["bash", "-n", SCRIPTS / name], check=True)
        deployer = (SCRIPTS / "valheim-modpack-deploy").read_text(encoding="utf-8")
        self.assertIn("--enable-dashboard-actions", deployer)
        self.assertIn("refusing to enable watchers while a request is pending", deployer)

    def test_single_action_watcher_covers_all_fixed_requests(self) -> None:
        path_unit = (SYSTEMD / "valheim-dashboard-actions.path").read_text(
            encoding="utf-8"
        )
        for request in (
            "manifest-request.json",
            "restart-request.json",
            "rollback-request.json",
            "world-restore-request.json",
        ):
            with self.subTest(request=request):
                self.assertIn(
                    f"PathExists=/var/lib/valheim-dashboard/pending/{request}",
                    path_unit,
                )
        service_unit = (SYSTEMD / "valheim-dashboard-actions.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("TimeoutStartSec=15min", service_unit)
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-dashboard-actions", service_unit
        )

    def test_canonical_server_units_replace_modpack_drop_ins(self) -> None:
        server_unit = (SERVER_SYSTEMD / "valheim.service").read_text(encoding="utf-8")
        restart_unit = (SERVER_SYSTEMD / "valheim-restart.service").read_text(
            encoding="utf-8"
        )
        timer_unit = (SERVER_SYSTEMD / "valheim-restart.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-server-launch", server_unit
        )
        self.assertNotIn("EnvironmentFile=", server_unit)
        self.assertIn(
            "Requires=valheim-world-restore-recovery.service", server_unit
        )
        self.assertIn("ReadWritePaths=/opt/valheim/modpack", server_unit)
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-maintenance", restart_unit
        )
        self.assertIn("OnCalendar=*-*-* 00,12:00:00 Asia/Shanghai", timer_unit)
        self.assertTrue((SERVER_SCRIPTS / "provision-valheim-server").is_file())
        self.assertFalse((SYSTEMD / "units").exists())
        self.assertFalse((SYSTEMD / "valheim.service.d").exists())
        self.assertFalse((SYSTEMD / "valheim-restart.service.d").exists())

    def test_restart_helpers_reject_invalid_cooldown_state(self) -> None:
        for name in ("valheim-restart-request", "valheim-rollback-request"):
            with self.subTest(name=name):
                contents = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertIn("cooldown_state_invalid", contents)
                self.assertNotIn("except Exception", contents)
