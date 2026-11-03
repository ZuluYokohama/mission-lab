"""Metadata-only archive boundaries and explicit incomplete snapshots."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.mission_lab_v0_1.core import LabError
from research.mission_lab_v0_1.inventory import PACKAGE_ROOT, inspect_archive


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.evidence = PACKAGE_ROOT / "evidence"
        self.evidence.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-test-", dir=self.evidence)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.archive = self.base / "archive"
        self.archive.mkdir()

    def collect(self, **kwargs):
        output = self.base / "output"
        result = inspect_archive(self.archive, output, allowed_roots=(self.archive,), **kwargs)
        return json.loads((result / "inventory.json").read_text(encoding="utf-8"))

    def test_regular_files_metadata_without_opening_source_bytes(self):
        source = self.archive / "MODEL.GGUF"
        source.write_bytes(b"not a model; only metadata")
        (self.archive / "empty").mkdir()
        original_open = Path.open

        def confined_open(path, *args, **kwargs):
            if path == source:
                self.fail("inventory tried to open archive file bytes")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", confined_open):
            report = self.collect()
        self.assertTrue(report["complete"])
        self.assertFalse(report["file_contents_read"])
        self.assertEqual(report["counts"]["files"], 1)
        record = next(entry for entry in report["entries"] if entry["kind"] == "file")
        self.assertEqual(record["path"], "MODEL.GGUF")
        self.assertEqual(record["bytes"], len(b"not a model; only metadata"))
        self.assertEqual(record["extension"], ".gguf")
        self.assertTrue(record["mtime_utc"].endswith("+00:00"))

    def test_entry_limit_discloses_incomplete_even_at_exact_limit(self):
        (self.archive / "a.txt").write_text("a", encoding="utf-8")
        report = self.collect(max_entries=1)
        self.assertFalse(report["complete"])
        self.assertIn("max_entries_reached", report["incomplete_reasons"])
        self.assertEqual(report["counts"]["entries"], 1)

    def test_depth_limit_does_not_open_descendants(self):
        nested = self.archive / "nested"
        nested.mkdir()
        (nested / "hidden.txt").write_text("hidden", encoding="utf-8")
        report = self.collect(max_depth=1)
        self.assertFalse(report["complete"])
        self.assertIn("max_depth_reached", report["incomplete_reasons"])
        self.assertEqual([entry["path"] for entry in report["entries"]], ["nested"])

    def test_unauthorized_and_non_directory_roots_rejected(self):
        with self.assertRaises(LabError):
            inspect_archive(self.archive, self.base / "bad-default")
        file_root = self.archive / "file.txt"
        file_root.write_text("x", encoding="utf-8")
        with self.assertRaises(LabError):
            inspect_archive(file_root, self.base / "bad-file", allowed_roots=(self.archive,))

    def test_malformed_bounds_rejected(self):
        for key, value in (("max_entries", True), ("max_entries", 0), ("max_depth", 65), ("max_depth", "2")):
            with self.subTest(key=key, value=value), self.assertRaises(LabError):
                self.collect(**{key: value})

    def test_cannot_overwrite_inventory_output(self):
        self.collect()
        with self.assertRaises((LabError, FileExistsError)):
            self.collect()

    def test_unsafe_output_path_refused(self):
        with self.assertRaises(LabError):
            inspect_archive(self.archive, PACKAGE_ROOT / "unsafe-output", allowed_roots=(self.archive,))

    def test_missing_root_refused(self):
        with self.assertRaises(LabError):
            inspect_archive(self.archive / "missing", self.base / "missing-out", allowed_roots=(self.archive,))

    def test_reparse_root_refused(self):
        with patch("research.mission_lab_v0_1.inventory._is_reparse", return_value=True):
            with self.assertRaises(LabError):
                self.collect()

    def test_symlink_escape_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.archive / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Host does not permit test symlink creation")
        with self.assertRaises(LabError):
            self.collect()


if __name__ == "__main__":
    unittest.main()
