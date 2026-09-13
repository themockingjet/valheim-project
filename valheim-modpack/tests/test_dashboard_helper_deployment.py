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
            "valheim-update-request",
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
            "update-request.json",
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
        self.assertIn("TimeoutStartSec=40min", service_unit)
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-dashboard-actions", service_unit
        )

    def test_status_publisher_timer_refreshes_before_snapshot_is_stale(self) -> None:
        service_unit = (SYSTEMD / "valheim-dashboard-status.service").read_text(
            encoding="utf-8"
        )
        timer_unit = (SYSTEMD / "valheim-dashboard-status.timer").read_text(
            encoding="utf-8"
        )
        deployer = (SCRIPTS / "valheim-modpack-deploy").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/valheim/modpack/lib/status_snapshot.py",
            service_unit,
        )
        self.assertIn("ProtectSystem=strict", service_unit)
        self.assertIn("ReadWritePaths=/var/lib/valheim-dashboard", service_unit)
        self.assertIn("OnUnitActiveSec=1min", timer_unit)
        self.assertIn("Persistent=true", timer_unit)
        self.assertIn("valheim-dashboard-status.timer", deployer)
        self.assertIn("systemctl start valheim-dashboard-status.service", deployer)

    def test_canonical_server_units_replace_modpack_drop_ins(self) -> None:
        server_unit = (SERVER_SYSTEMD / "valheim.service").read_text(encoding="utf-8")
        restart_unit = (SERVER_SYSTEMD / "valheim-restart.service").read_text(
            encoding="utf-8"
        )
        announcement_unit = (
            SERVER_SYSTEMD / "valheim-restart-announcement.service"
        ).read_text(encoding="utf-8")
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
        self.assertIn("RuntimeDirectory=valheim", server_unit)
        self.assertIn("TimeoutStopSec=8min", server_unit)
        self.assertNotIn("ExecStop=", server_unit)
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-restart-announcement",
            announcement_unit,
        )
        self.assertIn("User=valheim", announcement_unit)
        self.assertIn("TimeoutStartSec=16min", announcement_unit)
        self.assertIn("OnSuccess=valheim-restart.service", announcement_unit)
        self.assertIn("ReadWritePaths=/opt/valheim/modpack", server_unit)
        self.assertIn(
            "ExecStart=/usr/local/libexec/valheim-maintenance", restart_unit
        )
        self.assertIn("OnCalendar=*-*-* 11,23:45:00 Asia/Shanghai", timer_unit)
        self.assertIn("Unit=valheim-restart-announcement.service", timer_unit)
        self.assertTrue((SERVER_SCRIPTS / "provision-valheim-server").is_file())
        migration_script = (SERVER_SCRIPTS / "migrate-valheim-systemd").read_text(
            encoding="utf-8"
        )
        self.assertIn("valheim.service.d/launcher.conf", migration_script)
        self.assertIn("valheim-dashboard-actions.path", migration_script)
        self.assertIn(
            "refusing to migrate action watchers while a request is pending",
            migration_script,
        )
        self.assertIn("valheim-restart-announcement", migration_script)
        self.assertFalse((SYSTEMD / "units").exists())
        self.assertFalse((SYSTEMD / "valheim.service.d").exists())
        self.assertFalse((SYSTEMD / "valheim-restart.service.d").exists())

    def test_scheduled_restart_announcer_has_the_requested_countdown(self) -> None:
        announcer = (
            SERVER_SCRIPTS / "valheim-restart-announcement"
        ).read_text(encoding="utf-8")
        subprocess.run(
            ["bash", "-n", SERVER_SCRIPTS / "valheim-restart-announcement"],
            check=True,
        )
        for minutes in (15, 10, 5, 3, 1):
            with self.subTest(minutes=minutes):
                self.assertIn(f"scheduled-restart-{minutes}m", announcer)
                self.assertIn(
                    f"Scheduled server restart begins in {minutes} minute"
                    f"{'' if minutes == 1 else 's'}.",
                    announcer,
                )
        self.assertIn("scheduled-restart-now", announcer)
        self.assertEqual(announcer.count("/usr/bin/sleep 300"), 2)
        self.assertEqual(announcer.count("/usr/bin/sleep 120"), 2)
        self.assertIn("/usr/bin/sleep 60", announcer)

    def test_dashboard_deployer_requires_a_clean_root_revision(self) -> None:
        deployer = (
            PROJECT_ROOT / "scripts" / "dashboard" / "valheim-dashboard-deploy"
        ).read_text(encoding="utf-8")
        self.assertIn('git -C "$PROJECT_ROOT" rev-parse --verify HEAD', deployer)
        self.assertIn(
            "root repository is dirty; commit or remove all changes before deployment",
            deployer,
        )
        self.assertIn(
            'readonly RELEASE_ID=$(git -C "$PROJECT_ROOT" rev-parse --verify HEAD)',
            deployer,
        )

    def test_rollback_helper_rejects_invalid_cooldown_state(self) -> None:
        contents = (SCRIPTS / "valheim-rollback-request").read_text(encoding="utf-8")
        self.assertIn("cooldown_state_invalid", contents)
        self.assertNotIn("except Exception", contents)

    def test_deployer_seeds_the_lifecycle_announcer_socket_config(self) -> None:
        deployer = (SCRIPTS / "valheim-modpack-deploy").read_text(encoding="utf-8")
        config = (
            PROJECT_ROOT
            / "config"
            / "modpack"
            / "overrides"
            / "io.hexium.valheim.lifecycleannouncer.cfg"
        ).read_text(encoding="utf-8")
        self.assertIn("io.hexium.valheim.lifecycleannouncer.cfg", deployer)
        self.assertIn("Path = /run/valheim/lifecycle-announcer.sock", config)
