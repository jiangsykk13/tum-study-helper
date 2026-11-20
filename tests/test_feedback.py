"""Optional self-report informs estimates without fabricating observations."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import study_feedback


class FeedbackTest(unittest.TestCase):
    def test_requires_three_timing_observations_and_keeps_budget_external(self):
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            with tempfile.TemporaryDirectory(prefix="feedback-test-") as tmp:
                root = Path(tmp)
                (root / "计划").mkdir()
                progress = {"version": 1, "days": {}}
                for n in range(1, 4):
                    day = f"2030-01-0{n}"
                    (root / "计划" / f"{day}.json").write_text(json.dumps({"tasks": [{"id": "t", "title": "read",
                        "course_key": "IMG001", "course": "Imaginary", "section": "course", "kind": "lecture", "minutes": 30}]}), encoding="utf-8")
                    progress["days"][day] = {"t": {"state": "done", "actual_minutes": 45, "understanding": "hard"}}
                (root / "进度.json").write_text(json.dumps(progress), encoding="utf-8")
                before = (root / "进度.json").read_bytes()
                self.assertIsNone(study_feedback.summarize(root, "2030-01-02")["courses"][0]["suggested_time_factor"])
                summary = study_feedback.summarize(root, "2030-01-03")
                self.assertEqual(summary["courses"][0]["suggested_time_factor"], 1.5)
                self.assertEqual(summary["courses"][0]["actual_minutes"], 135)
                self.assertEqual((root / "进度.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
