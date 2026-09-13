"""Dependency resolution and lock generation tests."""

from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from src.errors import ModpackError
from src.manifest import ManifestPackage
from src.resolver import resolve


def zip_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.dll", b"test")
    return buffer.getvalue()


class FakeHexium:
    def __init__(self, packages: dict[tuple[str, str], dict]) -> None:
        self.packages = packages
        self.downloads: list[str] = []

    def get_package(self, namespace: str, name: str) -> dict:
        return self.packages[(namespace, name)]["package"]

    def get_version(self, namespace: str, name: str, version: str) -> dict:
        return self.packages[(namespace, name)]["versions"][version]

    def download(self, url: str) -> bytes:
        self.downloads.append(url)
        return zip_bytes()


def package(
    namespace: str, name: str, version: str, dependencies: list[str] | None = None
) -> dict:
    metadata = {
        "namespace": namespace,
        "name": name,
        "version_number": version,
        "full_name": f"{namespace}-{name}-{version}",
        "download_url": f"https://cdn.hexium.gg/{namespace}-{name}-{version}.zip",
        "dependencies": dependencies or [],
        "is_active": True,
    }
    return {
        "package": {
            "namespace": namespace,
            "name": name,
            "is_deprecated": False,
            "latest": metadata,
        },
        "versions": {version: metadata},
    }


class ResolverTests(unittest.TestCase):
    def resolve_with(self, packages: dict[tuple[str, str], dict], manifest: list[ManifestPackage]):
        with tempfile.TemporaryDirectory() as directory:
            return resolve(manifest, FakeHexium(packages), Path(directory))

    def test_resolves_dependencies_and_checksums_archives(self) -> None:
        packages = {
            ("Author", "Mod"): package(
                "Author", "Mod", "1.2.0", ["denikson-BepInExPack_Valheim-5.4.2350"]
            ),
            ("denikson", "BepInExPack_Valheim"): package(
                "denikson", "BepInExPack_Valheim", "5.4.2350"
            ),
        }
        lock = self.resolve_with(
            packages,
            [
                ManifestPackage(
                    "Author", "Mod", "latest", "server", allow_prerelease=False
                )
            ],
        )

        self.assertEqual(lock["source"], "hexium")
        self.assertEqual(len(lock["packages"]), 2)
        dependency = next(
            package for package in lock["packages"] if package["name"] == "BepInExPack_Valheim"
        )
        self.assertEqual(dependency["role"], "server")
        self.assertEqual(len(dependency["sha256"]), 64)

    def test_rejects_prerelease_without_manifest_opt_in(self) -> None:
        packages = {("Author", "Mod"): package("Author", "Mod", "1.2.0-beta.1")}
        with self.assertRaisesRegex(ModpackError, "Prerelease package is not allowed"):
            self.resolve_with(
                packages,
                [ManifestPackage("Author", "Mod", "latest", "server", False)],
            )

    def test_rejects_unsatisfied_dependency_minimum(self) -> None:
        packages = {
            ("Author", "Mod"): package(
                "Author", "Mod", "1.0.0", ["denikson-BepInExPack_Valheim-5.4.2351"]
            ),
            ("denikson", "BepInExPack_Valheim"): package(
                "denikson", "BepInExPack_Valheim", "5.4.2350"
            ),
        }
        with self.assertRaisesRegex(ModpackError, "does not satisfy dependency"):
            self.resolve_with(
                packages,
                [ManifestPackage("Author", "Mod", "latest", "server", False)],
            )

    def test_client_only_dependency_is_not_promoted_to_server(self) -> None:
        packages = {
            ("Author", "UiMod"): package(
                "Author", "UiMod", "1.0.0", ["denikson-BepInExPack_Valheim-5.4.2350"]
            ),
            ("denikson", "BepInExPack_Valheim"): package(
                "denikson", "BepInExPack_Valheim", "5.4.2350"
            ),
        }
        lock = self.resolve_with(
            packages,
            [ManifestPackage("Author", "UiMod", "latest", "client", False)],
        )
        self.assertEqual({item["role"] for item in lock["packages"]}, {"client"})
