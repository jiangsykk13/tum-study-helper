"""Offline tests for the course map parser. Run with the project's Python and -I."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC = importlib.util.spec_from_file_location("course_map", ROOT / "course_map.py")
course_map = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = course_map
SPEC.loader.exec_module(course_map)

TUM = Path(r"X:\Study")
SEMESTER = TUM / "Semester 1 (WS 2030-31)"


class CourseMapTest(unittest.TestCase):
    def setUp(self):
        text = (FIXTURES / "course_map.md").read_text(encoding="utf-8")
        self.entries = course_map.parse_course_map(text, TUM)

    def test_rows_with_ids_only(self):
        self.assertEqual([e.moodle_id for e in self.entries], [900001, 900002, 900003, 900004])

    def test_default_targets_and_artemis_id(self):
        first = self.entries[0]
        course_dir = SEMESTER / "IMG0001 Imaginary Algorithms"
        self.assertEqual(first.semester, "Semester 1 (WS 2030-31)")
        self.assertEqual(first.title, "Imaginary Algorithms (IMG0001)")
        self.assertEqual(first.artemis_id, 901)
        self.assertEqual(first.course_dir, str(course_dir))
        # Both platforms share the course's Official folder.
        self.assertEqual(first.moodle_dest, str(course_dir / "Official"))
        self.assertEqual(first.artemis_dest, str(course_dir / "Official"))
        self.assertEqual(first.inbox, str(course_dir / "_Inbox"))
        self.assertIsNone(self.entries[1].artemis_id)
        self.assertIsNone(self.entries[1].artemis_dest)

    def test_override_and_continuation_row(self):
        lecture, exercise = self.entries[2], self.entries[3]
        course_dir = SEMESTER / "IMG0003 Fictional Business"
        self.assertEqual(lecture.title, "Fictional Business – Lecture")
        self.assertEqual(lecture.moodle_dest, str(course_dir / "Official" / "Moodle Lecture"))
        self.assertEqual(exercise.folder, "IMG0003 Fictional Business")
        self.assertEqual(exercise.moodle_dest, str(course_dir / "Official" / "Moodle Exercise"))

    def test_completed_status(self):
        self.assertEqual([e.completed for e in self.entries], [False, True, False, False])
        marked = course_map.parse_course_map(
            "## S（`Semester 9`）\n| 文件夹 | 课程 | Moodle 编号 |\n|---|---|---|\n| `X` | Old Course（已完成） | 7 |", TUM)
        self.assertEqual((marked[0].completed, marked[0].title), (True, "Old Course"))
        self.assertTrue(self.entries[1].to_dict()["completed"])

    def test_load_uses_the_map_folder_as_root(self):
        entries = course_map.load_course_map(FIXTURES / "course_map.md")
        self.assertTrue(entries[0].course_dir.startswith(str(FIXTURES / "Semester 1")))
        with self.assertRaises(FileNotFoundError):
            course_map.load_course_map(FIXTURES / "missing.md")

    def test_empty_and_unrelated_text(self):
        self.assertEqual(course_map.parse_course_map("", TUM), [])
        self.assertEqual(course_map.parse_course_map("| a | b |\n|---|---|\n| 1 | 2 |", TUM), [])


if __name__ == "__main__":
    unittest.main()
