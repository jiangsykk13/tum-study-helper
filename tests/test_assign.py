"""Offline assignment parser tests with wholly fictional HTML fixtures."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("moodle_assign", ROOT / "moodle_assign.py")
assign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assign)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "assign"


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.blockers = [self.enterContext(patch(target, side_effect=AssertionError(
            "Network and credentials are forbidden in offline tests"))) for target in (
                "requests.adapters.HTTPAdapter.send", "socket.socket.connect",
                "socket.socket.connect_ex", "keyring.get_password", "keyring.set_password")]

    def tearDown(self):
        for blocker in self.blockers:
            blocker.assert_not_called()

    def parse_fixture(self, name):
        return assign.parse_assignment_page((FIXTURES / name).read_text(encoding="utf-8"))

    def test_submitted_graded_and_feedback(self):
        result = self.parse_fixture("submitted_graded.html")
        self.assertEqual(result["submission"], "submitted")
        self.assertEqual(result["grading"], "graded")
        self.assertEqual(result["grade"], "87 / 100")
        self.assertEqual(result["feedback_rows"][1], {
            "label": "Feedback comments", "value": "Clear explanation. Add a worked example."})
        self.assertEqual(len(result["raw_rows"]), 5)

    def test_dates_keep_original_words(self):
        result = self.parse_fixture("submitted_graded.html")
        self.assertEqual(result["opened"], "Monday, 1 June 2037, 09:00")
        self.assertEqual(result["due"], "Monday, 8 June 2037, 18:00")
        self.assertEqual(result["cutoff"], "Tuesday, 9 June 2037, 18:00")
        self.assertEqual(result["dates"][0]["text"], "Opened: Monday, 1 June 2037, 09:00")

    def test_new_and_overdue(self):
        result = self.parse_fixture("new_overdue.html")
        self.assertEqual(result["submission"], "new")
        self.assertEqual(result["grading"], "notgraded")
        self.assertEqual(result["status_rows"][2]["value"], "Assignment is overdue by 1 day")
        self.assertEqual(result["cutoff"], "Friday, 5 June 2037, 12:00")
        self.assertIsNone(result["grade"])

    def test_draft(self):
        result = self.parse_fixture("draft.html")
        self.assertEqual(result["submission"], "draft")
        self.assertEqual(result["grading"], "notgraded")
        self.assertEqual(result["due"], "2037-06-10 10:00")

    def test_missing_wrapper_keeps_raw_pairs(self):
        result = self.parse_fixture("missing_status.html")
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["grading"], "unknown")
        self.assertEqual(result["status_rows"], result["raw_rows"])
        self.assertEqual(result["raw_rows"][0]["label"], "Participation")
        self.assertEqual(result["dates"][0]["value"], "2037-06-03")
        self.assertIsNone(result["due"])

    def test_german_labels_and_decimal_comma(self):
        result = self.parse_fixture("german.html")
        self.assertEqual(result["submission"], "submitted")
        self.assertEqual(result["grading"], "graded")
        self.assertEqual(result["grade"], "1,7")
        self.assertEqual(result["due"], "Montag, 8. Juni 2037, 18:00")
        self.assertEqual(result["cutoff"], "Dienstag, 9. Juni 2037, 18:00")
        self.assertEqual(result["status_rows"][0]["label"], "Abgabestatus")

    def test_status_never_guesses_from_visible_words(self):
        result = assign.parse_assignment_page(
            "<div class='submissionstatustable'><table><tr><th>Submission status</th>"
            "<td>Submitted for grading</td></tr><tr><th>Grading status</th><td>Graded</td>"
            "</tr></table></div>")
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["grading"], "unknown")

    def test_foreign_labels_use_only_css_state(self):
        result = assign.parse_assignment_page(
            "<div class='submissionstatustable'><table><tr><th>任意标签</th>"
            "<td class='submissionstatusdraft overdue'>任意内容</td></tr>"
            "<tr><th>別の項目</th><td class='submissionnotgraded'>任意内容</td>"
            "</tr></table></div>")
        self.assertEqual(result["submission"], "draft")
        self.assertEqual(result["grading"], "notgraded")

    def test_empty_and_incomplete_pages(self):
        for html in ("", "<p>Only instructions</p>", "<table><tr><th>No value</th></tr></table>"):
            with self.subTest(html=html):
                result = assign.parse_assignment_page(html)
                self.assertEqual(result["submission"], "unknown")
                self.assertEqual(result["raw_rows"], [])
                self.assertIsNone(result["grade"])

    def test_feedback_does_not_become_submission_status(self):
        result = assign.parse_assignment_page(
            "<div class='feedback'><table><tr><th>Grade</th><td>75</td></tr></table></div>")
        self.assertEqual(result["status_rows"], [])
        self.assertEqual(result["grade"], "75")
        self.assertEqual(result["submission"], "unknown")

    def test_unrecognized_feedback_preserves_pairs(self):
        result = assign.parse_assignment_page(
            "<div class='feedback'><table><tr><th>Custom scoring</th><td>excellent</td>"
            "</tr></table></div>")
        self.assertIsNone(result["grade"])
        self.assertEqual(result["feedback_rows"], [{"label": "Custom scoring", "value": "excellent"}])

    def test_grade_css_and_empty_grade(self):
        for value, expected in (("42 / 50", "42 / 50"), ("-", None), ("–", None), ("", None)):
            with self.subTest(value=value):
                result = assign.parse_assignment_page(
                    "<div class='feedback'><table><tr><th>Unfamiliar label</th>"
                    f"<td class='gradevalue'>{value}</td></tr></table></div>")
                self.assertEqual(result["grade"], expected)

    def test_hidden_and_executable_markup_is_not_returned(self):
        result = assign.parse_assignment_page(
            "<div class='submissionstatustable'><table><tr><th>Status</th>"
            "<td class='submissionstatussubmitted'>Visible<span class='visually-hidden'>Hidden</span>"
            "<script>untrusted()</script><style>hidden{}</style><nav>Navigation</nav>"
            "<span class='sr-only'>Hidden too</span><span hidden>Invisible text</span>"
            "<input type='hidden' value='synthetic-hidden-field'></td></tr></table></div>")
        self.assertEqual(result["status_rows"][0]["value"], "Visible")
        self.assertEqual(result["submission"], "submitted")

    def test_conflicting_css_state_is_unknown(self):
        result = assign.parse_assignment_page(
            "<table><tr><th>Status</th><td class='submissionstatusdraft submissionstatussubmitted'>"
            "Ambiguous</td></tr><tr><th>Grade status</th>"
            "<td class='submissiongraded submissionnotgraded'>Ambiguous</td></tr></table>")
        self.assertEqual(result["submission"], "unknown")
        self.assertEqual(result["grading"], "unknown")

    def test_outside_main_tables_are_ignored(self):
        result = assign.parse_assignment_page(
            "<table><tr><th>Navigation field</th><td>Ignore</td></tr></table>"
            "<main id='region-main'><table><tr><th>Assignment field</th><td>Keep</td></tr></table></main>")
        self.assertEqual(result["raw_rows"], [{"label": "Assignment field", "value": "Keep"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
