"""A changed attempt and a repeated score are different events."""
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
import practice


class AssessmentTest(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="assessment-test-")))
        self.folder = self.root / "problem"
        self.folder.mkdir()
        self.records = self.root / "练习记录.json"
        self.records.write_text(json.dumps({"version": 1, "problems": [{"date": "2030-01-01",
            "folder": str(self.folder), "difficulty": 1, "skeleton_sha256": "framework", "last_result": None}]}), encoding="utf-8")

    def result(self, sha="new-answer", passed=8):
        return {"compiled": True, "total": 8, "passed": passed, "failed": [], "code_sha256": sha,
                "checked_at": "2030-01-01T00:00:00+01:00"}

    def test_same_code_only_changes_difficulty_once(self):
        with patch.object(practice, "run_tests", return_value=self.result()):
            first = practice.record_result(self.root, self.folder)
            second = practice.record_result(self.root, self.folder)
        self.assertTrue(first["difficulty_adjusted"])
        self.assertFalse(second["difficulty_adjusted"])
        self.assertEqual(second["next_difficulty"], 2)

    def test_zero_pass_attempt_is_not_untouched_framework(self):
        with patch.object(practice, "run_tests", return_value=self.result("different-code", 0)):
            self.assertTrue(practice.record_result(self.root, self.folder)["attempted"])
        with patch.object(practice, "run_tests", return_value=self.result("framework", 0)):
            result = practice.record_result(self.root, self.folder)
        self.assertFalse(result["attempted"])
        self.assertFalse(result["difficulty_adjusted"])

    def test_old_record_without_baseline_does_not_guess_framework(self):
        data = json.loads(self.records.read_text())
        data["problems"][0].pop("skeleton_sha256")
        self.records.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(practice, "run_tests", return_value=self.result()):
            result = practice.record_result(self.root, self.folder)
        self.assertTrue(result["baseline_missing"])
        self.assertFalse(result["difficulty_adjusted"])

    def test_existing_day_cannot_register_different_problem(self):
        with self.assertRaises(ValueError):
            practice.register_problem(self.root, self.root / "second", day="2030-01-01", title="t", topic="t")


if __name__ == "__main__":
    unittest.main()
