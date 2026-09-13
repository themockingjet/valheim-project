"""Tests for bounded fixed-source Hexium catalogue search."""

import unittest
from unittest.mock import patch

from src.hexium_catalog import CatalogError, HexiumCatalog, InvalidSearch


def catalogue() -> list[dict[str, object]]:
    return [
        {
            "owner": "denikson",
            "name": "BepInExPack_Valheim",
            "is_deprecated": False,
            "versions": [
                {
                    "version_number": "5.4.2350",
                    "is_active": True,
                    "description": "Runtime support",
                    "dependencies": [],
                    "download_url": "https://unexposed.invalid/archive.zip",
                }
            ],
        },
        {
            "owner": "Other",
            "name": "BetterWards",
            "is_deprecated": False,
            "versions": [
                {
                    "version_number": "1.2.3",
                    "is_active": True,
                    "description": "x" * 500,
                    "dependencies": ["one", "two"],
                }
            ],
        },
        {
            "owner": "Old",
            "name": "DeprecatedMod",
            "is_deprecated": True,
            "versions": [],
        },
    ]


class HexiumCatalogTests(unittest.TestCase):
    def test_search_returns_allowlisted_bounded_results(self) -> None:
        with patch("src.hexium_catalog._fetch_catalog", return_value=catalogue()):
            results = HexiumCatalog().search("wards")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["namespace"], "Other")
        self.assertEqual(results[0]["name"], "BetterWards")
        self.assertEqual(results[0]["latest_version"], "1.2.3")
        self.assertEqual(results[0]["dependency_count"], 2)
        self.assertLessEqual(len(results[0]["description"]), 240)
        self.assertNotIn("download_url", results[0])

    def test_search_rejects_unsafe_or_too_short_queries(self) -> None:
        catalog = HexiumCatalog()
        for query in ("x", "../../etc", "https://example.invalid"):
            with self.assertRaises(InvalidSearch):
                catalog.search(query)

    def test_search_uses_a_ttl_cache(self) -> None:
        with patch("src.hexium_catalog._fetch_catalog", return_value=catalogue()) as fetch:
            catalog = HexiumCatalog()
            catalog.search("bep")
            catalog.search("wards")
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
