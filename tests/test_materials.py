"""Interrupted handoff, partial reading and version relocation; no network."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study_materials as materials


class MaterialsTest(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="materials-test-")))
        self.root, self.dest = self.tmp / "Planner", self.tmp / "Official"
        self.dest.mkdir()
        self.path = self.dest / "slides.pdf"
        self.path.write_bytes(b"a fictional pdf")
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.report = {"dry_run": False, "course_id": 17, "artifacts": [{"identity": "17/1/slides.pdf",
                       "course_id": 17, "cmid": 1, "url": "https://www.moodle.tum.de/pluginfile.php/1/slides.pdf",
                       "local_path": "slides.pdf", "sha256": self.sha, "size": self.path.stat().st_size}], "errors": []}

    def ingest(self):
        materials.ingest(self.root, self.report, self.dest, "moodle", "IMG0001")
        return materials.pending(self.root)["items"][0]["id"]

    def test_downloaded_unchanged_report_does_not_consume_material(self):
        key = self.ingest()
        self.report["unchanged"] = {"count": 1, "files": ["slides.pdf"]}
        self.ingest()
        queue = materials.pending(self.root)
        self.assertEqual((queue["total"], queue["items"][0]["id"]), (1, key))

    def test_partial_pages_survive_and_cannot_be_prematurely_consumed(self):
        key = self.ingest()
        materials.record_summary(self.root, key, "first ten pages", pages=[1, 10], page_count=20)
        with self.assertRaises(materials.MaterialError):
            materials.record_summary(self.root, key, "unfinished", complete=True)
        item = materials.resolve(self.root, key)
        self.assertEqual((item["status"], item["read_ranges"]), ("pending", [[1, 10]]))
        materials.record_summary(self.root, key, "last ten pages", pages=[11, 20], complete=True)
        self.assertEqual(materials.resolve(self.root, key)["status"], "summarized")
        self.assertEqual(materials.pending(self.root)["total"], 1)  # no published plan yet

    def test_publish_then_crash_is_reconciled_from_plan(self):
        key = self.ingest()
        materials.record_summary(self.root, key, "all", pages=[1, 20], page_count=20, complete=True)
        folder = self.root / "计划"
        folder.mkdir()
        (folder / "2030-01-01.json").write_text(json.dumps({"tasks": [{"materials": [
            {"id": key, "version": self.sha, "path": str(self.path)}]}]}), encoding="utf-8")
        self.assertEqual(materials.pending(self.root)["total"], 0)

    def test_replaced_material_keeps_historical_version_addressable(self):
        key = self.ingest()
        old = self.dest / "slides_old.pdf"
        self.path.rename(old)
        self.path.write_bytes(b"revised pdf")
        art = self.report["artifacts"][0]
        art["history"] = [{"local_path": old.name, "sha256": self.sha, "size": old.stat().st_size}]
        art.update(sha256=materials.digest(self.path), size=self.path.stat().st_size)
        materials.ingest(self.root, self.report, self.dest, "moodle", "IMG0001")
        self.assertEqual(materials.resolve(self.root, key)["path"], str(old))

    def test_modified_file_not_used_and_preview_does_not_write(self):
        preview = dict(self.report, dry_run=True)
        with self.assertRaises(materials.MaterialError):
            materials.ingest(self.root, preview, self.dest, "moodle", "IMG0001")
        self.assertFalse(self.root.exists())
        key = self.ingest()
        self.path.write_bytes(b"corrupt")
        self.assertEqual(materials.pending(self.root)["total"], 0)
        self.assertEqual(len(materials.pending(self.root)["unavailable"]), 1)
        with self.assertRaises(materials.MaterialError):
            materials.resolve(self.root, key)

    def test_unsafe_report_and_damaged_state_are_not_saved(self):
        self.report["artifacts"][0]["url"] += "?sesskey=private-value"
        with self.assertRaises(materials.MaterialError):
            self.ingest()
        self.assertFalse((self.root / materials.INDEX).exists())
        self.report["artifacts"][0]["url"] = "https://www.moodle.tum.de/pluginfile.php/a.pdf"
        self.root.mkdir()
        (self.root / materials.INDEX).write_text("broken", encoding="utf-8")
        with self.assertRaises(materials.MaterialError):
            self.ingest()
        self.assertEqual((self.root / materials.INDEX).read_text(), "broken")

    def test_json_and_encoded_session_values_are_rejected(self):
        key = self.ingest()
        for text in ('{"sesskey": "private-value"}', 'sesskey%253Dprivate-value'):
            with self.assertRaises(materials.MaterialError):
                materials.record_summary(self.root, key, text)

    def test_bad_index_shape_is_reported_and_malformed_plan_is_ignored(self):
        self.ingest()
        folder = self.root / "计划"
        folder.mkdir()
        (folder / "2030-01-01.json").write_text('{"tasks":[null,{"materials":null}]}', encoding="utf-8")
        self.assertEqual(materials.pending(self.root)["total"], 1)
        data = json.loads((self.root / materials.INDEX).read_text(encoding="utf-8"))
        next(iter(data["items"].values())).pop("first_seen")
        (self.root / materials.INDEX).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(materials.MaterialError):
            materials.pending(self.root)


if __name__ == "__main__":
    unittest.main()
