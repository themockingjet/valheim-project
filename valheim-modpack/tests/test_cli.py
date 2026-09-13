"""Tests for non-destructive status and retention helpers."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest

from src.cli import _gc_command, _positive_int, _status_command, build_parser


class Arguments:
    def __init__(self, root: Path, keep: int = 3) -> None:
        self.root = root
        self.cache = root / "cache"
        self.lock = root / "modpack.lock.json"
        self.keep = keep


class CliTests(unittest.TestCase):
    def test_retention_count_must_be_positive(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            _positive_int("0")

    def make_release(self, releases: Path, name: str) -> Path:
        release = releases / name
        release.mkdir(parents=True)
        (release / "release-lock.json").write_text(
            json.dumps({"schema_version": 1, "packages": [f"{name}.zip"]}),
            encoding="utf-8",
        )
        return release

    def test_status_reports_no_active_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_status_command(Arguments(Path(directory))), 0)

    def test_gc_retains_current_and_previous_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            oldest = self.make_release(releases, "oldest")
            previous = self.make_release(releases, "previous")
            current = self.make_release(releases, "current")
            (root / "current").symlink_to(current)
            (root / "previous").symlink_to(previous)
            cache = root / "cache"
            cache.mkdir()
            (cache / "orphan.zip").write_bytes(b"orphan")

            self.assertEqual(_gc_command(Arguments(root, keep=1)), 0)

            self.assertFalse(oldest.exists())
            self.assertTrue(previous.exists())
            self.assertTrue(current.exists())
            self.assertFalse((cache / "orphan.zip").exists())

    def test_apply_pending_manifest_subcommand_requires_a_request_path(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["apply-pending-manifest"])

    def test_apply_pending_manifest_subcommand_parses_required_arguments(self) -> None:
        parser = build_parser()
        arguments = parser.parse_args(
            ["apply-pending-manifest", "--request", "/tmp/manifest-request.json"]
        )
        self.assertEqual(arguments.request, Path("/tmp/manifest-request.json"))
        self.assertEqual(arguments.command, "apply-pending-manifest")
        self.assertTrue(callable(arguments.handler))
