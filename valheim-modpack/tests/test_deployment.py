"""Release staging and activation tests."""

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from src.deployment import (
    LockedPackage,
    activate_release,
    rollback,
    stage_release,
)
from src.errors import ModpackError


def archive_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


class FakeClient:
    def __init__(self, archives: dict[str, bytes]) -> None:
        self.archives = archives

    def download(self, url: str) -> bytes:
        return self.archives[url]


def locked(
    namespace: str, name: str, version: str, archive: bytes, role: str = "server"
) -> LockedPackage:
    import hashlib

    full_name = f"{namespace}-{name}-{version}"
    return LockedPackage(
        namespace,
        name,
        version,
        full_name,
        f"https://cdn.hexium.gg/{full_name}.zip",
        hashlib.sha256(archive).hexdigest(),
        role,
    )


class DeploymentTests(unittest.TestCase):
    def base_archive(self) -> bytes:
        return archive_bytes(
            {
                "BepInExPack_Valheim/BepInEx/core/BepInEx.Preloader.dll": b"loader",
                "BepInExPack_Valheim/BepInEx/config/BepInEx.cfg": b"config",
                "BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so": b"doorstop",
                "manifest.json": b"{}",
            }
        )

    def test_stages_base_mod_and_config_without_activation(self) -> None:
        base = self.base_archive()
        mod = archive_bytes({"plugins/Example.dll": b"mod"})
        packages = [
            locked("denikson", "BepInExPack_Valheim", "5.4.2350", base),
            locked("Author", "ExampleMod", "1.0.0", mod),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overrides = root / "config-overrides"
            overrides.mkdir()
            (overrides / "Example.cfg").write_text("preserved", encoding="utf-8")
            client = FakeClient({package.download_url: archive for package, archive in zip(packages, (base, mod))})
            release = stage_release(
                packages,
                releases_directory=root / "releases",
                cache_directory=root / "cache",
                config_overrides=overrides,
                client=client,
            )

            self.assertFalse((root / "current").exists())
            self.assertTrue((release / "BepInEx/core/BepInEx.Preloader.dll").is_file())
            self.assertEqual(
                (release / "BepInEx/plugins/Author-ExampleMod-1.0.0/plugins/Example.dll").read_bytes(),
                b"mod",
            )
            self.assertEqual(
                (release / "BepInEx/config/Example.cfg").read_text(encoding="utf-8"),
                "preserved",
            )

    def test_rejects_checksum_mismatch(self) -> None:
        base = self.base_archive()
        package = locked("denikson", "BepInExPack_Valheim", "5.4.2350", base)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ModpackError, "SHA-256 mismatch"):
                stage_release(
                    [package],
                    releases_directory=Path(directory) / "releases",
                    cache_directory=Path(directory) / "cache",
                    config_overrides=Path(directory) / "config-overrides",
                    client=FakeClient({package.download_url: b"not the locked archive"}),
                )

    def test_activation_and_rollback_swap_atomic_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            first = releases / "release-one"
            second = releases / "release-two"
            for release in (first, second):
                release.mkdir(parents=True)
                (release / "release-lock.json").write_text(json.dumps({}), encoding="utf-8")

            activate_release(root, first)
            activate_release(root, second)
            restored = rollback(root)

            self.assertEqual(restored, first.resolve())
            self.assertEqual((root / "current").resolve(), first.resolve())
            self.assertEqual((root / "previous").resolve(), second.resolve())
