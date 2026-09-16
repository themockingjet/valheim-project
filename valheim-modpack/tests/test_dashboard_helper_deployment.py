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
            "valheim-config-apply",
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
            "config-request.json",
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
            "WorkingDirectory=/opt/valheim/modpack/lib",
            service_unit,
        )
        self.assertIn(
            "ExecStart=/usr/bin/python3 -m src.status_snapshot",
            service_unit,
        )
        self.assertIn("ProtectSystem=strict", service_unit)
        self.assertIn("ReadWritePaths=/var/lib/valheim-dashboard", service_unit)
        self.assertIn("OnUnitActiveSec=1min", timer_unit)
        self.assertIn("Persistent=true", timer_unit)
        self.assertIn("valheim-dashboard-status.timer", deployer)
        self.assertIn("systemctl start valheim-dashboard-status.service", deployer)
        maintenance = (SCRIPTS / "valheim-maintenance").read_text(encoding="utf-8")
        self.assertIn('cd -- "$MODPACK_LIBRARY"', maintenance)
        self.assertIn("/usr/bin/python3 -m src.status_snapshot", maintenance)

    def test_world_backup_helpers_use_the_deployed_module_path(self) -> None:
        deployed_module = "/opt/valheim/modpack/lib/src/world_backups.py"
        for helper_name in (
            "valheim-world-backup-inventory",
            "valheim-world-restore",
            "valheim-world-restore-recover",
        ):
            with self.subTest(helper=helper_name):
                helper = (SCRIPTS / helper_name).read_text(encoding="utf-8")
                self.assertIn(deployed_module, helper)

    def test_inventory_failure_does_not_block_dashboard_action_deployment(self) -> None:
        deployer = (SCRIPTS / "valheim-modpack-deploy").read_text(encoding="utf-8")
        self.assertIn(
            "world-backup inventory is unavailable; continuing without restore inventory",
            deployer,
        )
        self.assertIn(
            "if ! /usr/bin/systemctl start valheim-dashboard-world-backup-inventory.service; then",
            deployer,
        )

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
                self.assertIn(f"send_burst 'scheduled-restart-{minutes}m'", announcer)
        self.assertIn("scheduled-restart-now", announcer)
        self.assertIn('send "${warning_id}-${attempt}" "$message"', announcer)
        self.assertIn("for attempt in 1 2 3", announcer)
        self.assertEqual(announcer.count("/usr/bin/sleep 294"), 2)
        self.assertEqual(announcer.count("/usr/bin/sleep 114"), 2)
        self.assertIn("/usr/bin/sleep 54", announcer)
        self.assertIn("send 'scheduled-restart-now'", announcer)

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

    def test_dashboard_deployer_resolves_npm_from_source_owner_environment(self) -> None:
        deployer_path = PROJECT_ROOT / "scripts" / "dashboard" / "valheim-dashboard-deploy"
        deployer = deployer_path.read_text(encoding="utf-8")
        subprocess.run(["bash", "-n", deployer_path], check=True)
        self.assertIn(
            "/usr/sbin/runuser -u \"$SOURCE_OWNER\" -- \\",
            deployer,
        )
        self.assertIn(
            'HOME="$SOURCE_HOME" USER="$SOURCE_OWNER" LOGNAME="$SOURCE_OWNER"',
            deployer,
        )
        self.assertIn("node_path=$(nvm which default 2>/dev/null)", deployer)
        self.assertIn('npm_path="${node_path%/node}/npm"', deployer)
        self.assertIn('readonly SOURCE_NODE_BIN=$(/usr/bin/dirname -- "$SOURCE_NPM")', deployer)
        self.assertIn('PATH=$SOURCE_NODE_BIN:$PATH', deployer)
        self.assertIn('run_npm_as_source_owner ci', deployer)
        self.assertIn('readonly SOURCE_NPM', deployer)
        self.assertNotIn("run_as_source_owner /usr/bin/npm", deployer)

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

    def test_deployer_seeds_the_discord_notifier_config_without_a_webhook(self) -> None:
        deployer = (SCRIPTS / "valheim-modpack-deploy").read_text(encoding="utf-8")
        config = (
            PROJECT_ROOT
            / "config"
            / "modpack"
            / "overrides"
            / "io.hexium.valheim.discordnotifier.cfg"
        ).read_text(encoding="utf-8")
        self.assertIn("io.hexium.valheim.discordnotifier.cfg", deployer)
        self.assertIn("DISCORD_NOTIFIER_ACTIVE", deployer)
        self.assertIn("WebhookUrl =", config)
        self.assertNotIn("https://discord.com/api/webhooks/", config)
