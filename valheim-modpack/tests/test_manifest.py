"""Manifest validation tests."""

from pathlib import Path
import tempfile
import unittest

from src.errors import ModpackError
from src.manifest import load_manifest


class ManifestTests(unittest.TestCase):
    def load(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.yaml"
            path.write_text(content, encoding="utf-8")
            return load_manifest(path)

    def test_loads_hexium_server_package(self) -> None:
        packages = self.load(
            """
schema_version: 1
packages:
  - source: hexium
    namespace: denikson
    name: BepInExPack_Valheim
    version: "5.4.2350"
"""
        )
        self.assertEqual(packages[0].role, "server")
        self.assertFalse(packages[0].allow_prerelease)

    def test_rejects_non_hexium_source(self) -> None:
        with self.assertRaisesRegex(ModpackError, "source must be 'hexium'"):
            self.load(
                """
schema_version: 1
packages:
  - source: thunderstore
    namespace: denikson
    name: BepInExPack_Valheim
    version: latest
"""
            )

    def test_rejects_duplicate_packages(self) -> None:
        with self.assertRaisesRegex(ModpackError, "Duplicate manifest package"):
            self.load(
                """
schema_version: 1
packages:
  - source: hexium
    namespace: denikson
    name: BepInExPack_Valheim
    version: latest
  - source: hexium
    namespace: denikson
    name: BepInExPack_Valheim
    version: latest
"""
            )
