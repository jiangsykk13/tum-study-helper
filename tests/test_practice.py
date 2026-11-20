"""Tests for practice.py: compile a tiny Java problem and run its JUnit tests. Run with the project's Python and -I.

Needs javac/java and the JUnit jar in <study root>\\Planner\\lib; skipped when either is missing.
No network. The problem is written to the system temp folder.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("practice", ROOT / "practice.py")
practice = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = practice
SPEC.loader.exec_module(practice)

READY = bool(shutil.which("javac") and shutil.which("java") and practice.JUNIT.is_file())

TEST = """import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import static org.junit.jupiter.api.Assertions.assertEquals;

@Timeout(2)
class SumTest {
    @Test void emptyArrayGivesZero() { assertEquals(0, Sum.of(new int[0])); }
    @Test void addsAllValues() { assertEquals(6, Sum.of(new int[] {1, 2, 3})); }
    @Test void negativeValues() { assertEquals(-1, Sum.of(new int[] {2, -3})); }
}
"""
SOLUTION = "class Sum { static int of(int[] a) { int s = 0; for (int x : a) s += x; return s; } }\n"
SKELETON = "class Sum {\n    // 示例框架：返回数组之和\n    static int of(int[] a) { return 0; }\n}\n"


@unittest.skipUnless(READY, "javac/java or the JUnit jar is missing")
class PracticeTest(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="practice-test-"))) / "2026-10-23 Sum"
        for name, text in (("src/Sum.java", SKELETON), ("test/SumTest.java", TEST), ("答案/Sum.java", SOLUTION)):
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_solution_passes_and_skeleton_mostly_fails(self):
        solved = practice.run_tests(self.folder, solution=True)
        self.assertEqual((solved["compiled"], solved["total"], solved["passed"], solved["failed"]), (True, 3, 3, []))
        skeleton = practice.run_tests(self.folder)
        self.assertEqual((skeleton["total"], skeleton["passed"]), (3, 1))
        self.assertEqual(sorted(skeleton["failed"]), ["addsAllValues()", "negativeValues()"])
        self.assertEqual(sorted(p.name for p in self.folder.rglob("*")),
                         sorted(["src", "test", "答案", "Sum.java", "SumTest.java", "Sum.java"]))  # nothing written

    def test_runner_is_ascii_crlf_and_never_overwritten(self):
        runner = practice.write_runner(self.folder)
        data = runner.read_bytes()
        self.assertTrue(data.startswith(b"@echo off\r\n"))
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        data.decode("ascii")
        self.assertIn(b'"%~dp0."', data)
        with self.assertRaises(FileExistsError):
            practice.write_runner(self.folder)

    def test_compile_errors_are_reported_not_raised(self):
        (self.folder / "src" / "Sum.java").write_text("class Sum { static int of(int[] a) { return } }", encoding="utf-8")
        result = practice.run_tests(self.folder)
        self.assertFalse(result["compiled"])
        self.assertEqual((result["total"], result["passed"]), (3, 0))
        self.assertIn("编译失败", result["error"])

    def test_missing_files_and_jar(self):
        self.assertIn("JUnit", practice.run_tests(self.folder, junit=self.folder / "none.jar")["error"])
        shutil.rmtree(self.folder / "test")
        self.assertIn("缺少", practice.run_tests(self.folder)["error"])


if __name__ == "__main__":
    unittest.main()
