"""Tests for applying a dashboard-originated pending manifest request."""

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from src.errors import ModpackError
from src.manifest_apply import apply_pending_manifest


def zip_bytes() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.dll", b"test")
    return buffer.getvalue()


class FakeHexium:
    def __init__(self, packages: dict[tuple[str, str], dict]) -> None:
        self.packages = packages

    def get_package(self, namespace: str, name: str) -> dict:
        return self.packages[(namespace, name)]["package"]

    def get_version(self, namespace: str, name: str, version: str) -> dict:
        try:
            return self.packages[(namespace, name)]["versions"][version]
        except KeyError as error:
            raise ModpackError(
                f"Hexium has no such version: {namespace}-{name}-{version}"
            ) from error

    def download(self, url: str) -> bytes:
        return zip_bytes()


def package(namespace: str, name: str, version: str) -> dict:
    metadata = {
        "namespace": namespace,
        "name": name,
        "version_number": version,
        "full_name": f"{namespace}-{name}-{version}",
        "download_url": f"https://cdn.hexium.gg/{namespace}-{name}-{version}.zip",
        "dependencies": [],
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


def valid_request(version: str = "1.0.0") -> dict:
    return {
        "schema_version": 1,
        "requested_at": "2026-09-13T05:00:00+00:00",
        "packages": [
            {
                "source": "hexium",
                "namespace": "denikson",
                "name": "BepInExPack_Valheim",
                "version": version,
                "channel": "stable",
                "role": "server",
            }
        ],
    }


def write_lock(path: Path, full_names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "hexium",
                "packages": [
                    {
                        "namespace": full_name.split("-")[0],
                        "name": full_name.split("-")[1],
                        "version": full_name.split("-")[2],
                        "full_name": full_name,
                        "download_url": f"https://cdn.hexium.gg/{full_name}.zip",
                        "sha256": "a" * 64,
                        "role": "server",
                    }
                    for full_name in full_names
                ],
            }
        ),
        encoding="utf-8",
    )


class ApplyPendingManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.manifest_path = self.root / "manifest.yaml"
        self.lock_path = self.root / "modpack.lock.json"
        self.cache_directory = self.root / "cache"
        self.client = FakeHexium(
            {("denikson", "BepInExPack_Valheim"): package("denikson", "BepInExPack_Valheim", "1.0.0")}
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def apply(self, request: dict) -> object:
        return apply_pending_manifest(
            request,
            manifest_path=self.manifest_path,
            lock_path=self.lock_path,
            cache_directory=self.cache_directory,
            client=self.client,
        )

    def test_applies_a_valid_request_and_writes_the_manifest(self) -> None:
        result = self.apply(valid_request())
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.package_count, 1)
        self.assertTrue(self.manifest_path.is_file())
        self.assertIn("denikson", self.manifest_path.read_text())

    def test_reports_added_and_removed_packages_relative_to_the_active_lock(self) -> None:
        write_lock(self.lock_path, ["denikson-BepInExPack_Valheim-0.9.0"])
        result = self.apply(valid_request("1.0.0"))
        self.assertIn("denikson-BepInExPack_Valheim-1.0.0", result.add)
        self.assertIn("denikson-BepInExPack_Valheim-0.9.0", result.remove)

    def test_reports_no_changes_when_the_lock_already_matches(self) -> None:
        write_lock(self.lock_path, ["denikson-BepInExPack_Valheim-1.0.0"])
        result = self.apply(valid_request("1.0.0"))
        self.assertEqual(result.add, [])
        self.assertEqual(result.remove, [])

    def test_missing_lock_reports_everything_as_added(self) -> None:
        result = self.apply(valid_request("1.0.0"))
        self.assertEqual(result.add, ["denikson-BepInExPack_Valheim-1.0.0"])
        self.assertEqual(result.remove, [])

    def test_corrupt_lock_is_treated_as_no_previous_packages(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("not json", encoding="utf-8")
        result = self.apply(valid_request("1.0.0"))
        self.assertEqual(result.add, ["denikson-BepInExPack_Valheim-1.0.0"])

    def test_rejects_wrong_schema_version(self) -> None:
        request = valid_request()
        request["schema_version"] = 2
        with self.assertRaises(ModpackError):
            self.apply(request)
        self.assertFalse(self.manifest_path.exists())

    def test_rejects_unsupported_top_level_field(self) -> None:
        request = valid_request()
        request["extra"] = True
        with self.assertRaises(ModpackError):
            self.apply(request)

    def test_rejects_non_hexium_source(self) -> None:
        request = valid_request()
        request["packages"][0]["source"] = "nexus"
        with self.assertRaises(ModpackError):
            self.apply(request)
        self.assertFalse(self.manifest_path.exists())

    def test_rejects_empty_packages(self) -> None:
        request = valid_request()
        request["packages"] = []
        with self.assertRaises(ModpackError):
            self.apply(request)

    def test_rejects_too_many_packages(self) -> None:
        request = valid_request()
        request["packages"] = request["packages"] * 51
        with self.assertRaises(ModpackError):
            self.apply(request)

    def test_rejects_invalid_package_shape(self) -> None:
        request = valid_request()
        del request["packages"][0]["role"]
        with self.assertRaises(ModpackError):
            self.apply(request)

    def test_unresolvable_manifest_does_not_modify_manifest_path(self) -> None:
        request = valid_request(version="9.9.9")
        with self.assertRaises(ModpackError):
            self.apply(request)
        self.assertFalse(self.manifest_path.exists())

    def test_invalid_namespace_is_rejected_by_the_authoritative_manifest_loader(self) -> None:
        request = valid_request()
        request["packages"][0]["namespace"] = "bad namespace!"
        with self.assertRaises(ModpackError):
            self.apply(request)


if __name__ == "__main__":
    unittest.main()
