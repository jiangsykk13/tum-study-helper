"""Offline tests for pure HTML rendering and label/course order extraction.

Run using the project's Python with -I. Every HTML/state sample is fictional.
Network and credential access are blocked, and bytecode output is disabled.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import keyring


sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "moodle_render.py"
SPEC = importlib.util.spec_from_file_location("moodle_render", SCRIPT)
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "render"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.blocked = [self.enterContext(patch(target, side_effect=AssertionError("Forbidden offline access")))
                        for target in ("requests.adapters.HTTPAdapter.send", "socket.socket.connect",
                                       "socket.socket.connect_ex")]
        self.blocked.extend(self.enterContext(patch.object(keyring, name, side_effect=AssertionError("Forbidden credential access")))
                            for name in ("get_password", "set_password"))

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()


class HtmlToTextTests(OfflineTestCase):
    def test_regular_table_heading_and_caption(self):
        text = render.html_to_text(fixture("tables.html"))
        self.assertIn("## Practice schedule", text)
        self.assertIn("Weekly plan\n| Week | Topic |\n| --- | --- |\n| 1 | Introduction |\n| 2 | Review |", text)
        self.assertTrue(text.endswith("After the table."))
        self.assertNotIn("Outside main region", text)

    def test_merged_cells_repeat_and_combine_headers(self):
        self.assertEqual(render.html_to_text(fixture("merged.html")),
                         "| Group | Results / First | Results / Second |\n"
                         "| --- | --- | --- |\n| A | 4 | 5 |\n| A | 6 | 6 |")

    def test_table_pipe_and_newline_escape(self):
        text = render.html_to_text(fixture("structured.html"))
        self.assertIn("| A \\| B | Line one<br>Line two<br>Paragraph three |", text)

    def test_backslash_before_pipe_cannot_unescape_pipe(self):
        text = render.html_to_text("<table><tr><th>Value</th></tr><tr><td>slash \\| pipe</td></tr></table>")
        self.assertIn("slash " + "\\" * 3 + "| pipe", text)

    def test_nested_lists_and_text_order(self):
        text = render.html_to_text(fixture("structured.html"))
        self.assertIn("- First item\n  - Nested item\n    - Deep item\n  Following text\n- Second item", text)

    def test_nested_list_inside_wrapper(self):
        text = render.html_to_text("<ul><li><p>Parent</p><div><ul><li>Child</li></ul></div></li></ul>")
        self.assertEqual(text, "- Parent\n  - Child")

    def test_hidden_text_scripts_styles_and_navigation_removed(self):
        text = render.html_to_text(fixture("structured.html"))
        for unwanted in ("Unwanted", "Never execute", "Nested hidden"):
            self.assertNotIn(unwanted, text)
        self.assertIn("# Fictional instructions", text)
        self.assertIn("Visible", text)

    def test_headerless_table_keeps_first_row(self):
        text = render.html_to_text("<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>")
        self.assertEqual(text, "|  |  |\n| --- | --- |\n| A | B |\n| C | D |")

    def test_row_header_table_keeps_labels_as_data(self):
        text = render.html_to_text("<table><tr><th>Status</th><td>Draft</td></tr><tr><th>Score</th><td>3</td></tr></table>")
        self.assertIn("| Status | Draft |", text)
        self.assertIn("| Score | 3 |", text)

    def test_ragged_rows_are_padded(self):
        text = render.html_to_text("<table><tr><th>A</th><th>B</th></tr><tr><td>Only</td></tr></table>")
        self.assertTrue(text.endswith("| Only |  |"))

    def test_invalid_spans_and_zero_rowspan_are_tolerated(self):
        text = render.html_to_text("<table><tr><th>A</th><th>B</th></tr>"
                                   "<tr><td rowspan='0'>Shared</td><td colspan='bad'>First</td></tr>"
                                   "<tr><td>Second</td></tr></table>")
        self.assertIn("| Shared | First |\n| Shared | Second |", text)

    def test_no_main_uses_body_and_ignores_comments(self):
        self.assertEqual(render.html_to_text("<html><head><title>Outside</title></head><body><h3>Topic</h3><!-- hidden --><p>Text</p></body></html>"),
                         "### Topic\n\nText")

    def test_empty_and_plain_text_documents(self):
        self.assertEqual(render.html_to_text(""), "")
        self.assertEqual(render.html_to_text("A &amp; B"), "A & B")

    def test_preformatted_text_preserves_line_indentation(self):
        self.assertEqual(render.html_to_text("<pre>one\n  two</pre>"), "one\n  two")


class LabelTests(OfflineTestCase):
    def sample(self):
        return {"cm": [
            {"id": 12, "name": "Label &amp; note", "module": "label", "sectionnumber": 1},
            {"id": 11, "name": "Notes", "module": "resource", "url": "https://www.moodle.tum.de/mod/resource/view.php?id=11"},
            {"id": 13, "name": "Later", "module": "label", "uservisible": False, "sectionnumber": 1},
            {"id": 21, "name": "First", "module": "label", "sectionnumber": 0},
            {"id": 14, "name": "Linked label", "module": "label", "url": "https://www.moodle.tum.de/mod/label/view.php?id=14"},
            {"id": 15, "name": "No URL forum", "module": "forum"},
        ], "section": [
            {"number": 1, "title": "Topic &amp; examples", "cmlist": [11, "12", 13, 14, 15]},
            {"number": 0, "title": "Start", "cmlist": [21]},
        ]}

    def test_extracts_labels_with_exact_activity_fields(self):
        labels = render.label_activities(self.sample(), 99)
        self.assertEqual([item["id"] for item in labels], [21, 12, 13])
        self.assertEqual(set(labels[0]), {"name", "type", "url", "section", "section_number", "available", "course_id", "id"})
        self.assertEqual(labels[1], {"id": 12, "name": "Label & note", "type": "label", "url": None,
                                     "section": "Topic & examples", "section_number": 1, "available": True, "course_id": 99})
        self.assertFalse(labels[2]["available"])

    def test_order_keeps_labels_between_ordinary_activities(self):
        order = render.activity_order(self.sample())
        combined = [11, 14] + [label["id"] for label in render.label_activities(self.sample(), 99)]
        self.assertEqual(sorted(combined, key=order.__getitem__), [21, 11, 12, 13, 14])

    def test_missing_cmlist_labels_append_to_matching_section(self):
        state = {"cm": [{"id": 1, "module": "resource", "url": "some URL"},
                         {"id": 2, "module": "label", "name": "End", "sectionnumber": 2}],
                 "section": [{"number": 2, "title": "Second", "cmlist": [1]}]}
        self.assertEqual(render.activity_order(state), {1: 0, 2: 1})
        self.assertEqual(render.label_activities(state, 1)[0]["section"], "Second")

    def test_missing_sections_use_module_section_number(self):
        state = {"cm": [{"id": "7", "module": "label", "name": "Note", "sectionnumber": "3", "uservisible": "0"}]}
        label = render.label_activities(state, 5)[0]
        self.assertEqual(label["section_number"], 3)
        self.assertEqual(label["section"], "")
        self.assertFalse(label["available"])

    def test_empty_and_malformed_entries_are_ignored(self):
        for state in ({}, {"cm": None, "section": None}, {"cm": [None, {}, {"module": "label", "id": "bad"}], "section": [None]}, None):
            with self.subTest(state=state):
                self.assertEqual(render.label_activities(state, 1), [])
                self.assertEqual(render.activity_order(state), {})

    def test_duplicate_cmlist_ids_do_not_duplicate_labels(self):
        state = {"cm": [{"id": 1, "name": "Once", "module": "label"}],
                 "section": [{"number": 0, "cmlist": [1, "1", 999]}]}
        self.assertEqual(len(render.label_activities(state, 1)), 1)
        self.assertEqual(render.activity_order(state), {1: 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
