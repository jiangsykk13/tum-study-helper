"""Offline tests for the planner files (plans read, ticks written). Run with the project's Python and -I.

Fixture plans in tests/fixtures/planner/ are fictional; every test works on a copy
in the system temp folder.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "planner"
SPEC = importlib.util.spec_from_file_location("planner", ROOT / "planner.py")
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

TODAY = date(2026, 10, 23)


def temporary_directory(case):
    # Inherit the private system-temp ACL during creation only (see test_offline.py).
    mkdir = os.mkdir
    with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
        return Path(case.enterContext(tempfile.TemporaryDirectory(prefix="planner-test-")))


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.root = temporary_directory(self) / "Planner"
        shutil.copytree(FIXTURE, self.root)
        self.progress = self.root / planner.PROGRESS_FILE

    def test_view_shows_todays_plan_and_unfinished_previous_tasks(self):
        planner.set_state(self.root, "2026-10-22", "iti-l02", "done")
        planner.set_state(self.root, "2026-10-22", "ca-lec02", "half")
        result = planner.view(self.root, today=TODAY)
        self.assertEqual(result["dates"], ["2026-10-22", "2026-10-23"])
        self.assertEqual(result["date"], "2026-10-23")
        self.assertEqual(len(result["plan"]["tasks"]), 5)
        self.assertEqual(result["states"], {})
        previous = result["previous"]
        self.assertEqual(previous["date"], "2026-10-22")
        self.assertEqual([t["id"] for t in previous["tasks"]], ["ca-lec02", "en-read"])
        self.assertEqual(previous["states"], {"ca-lec02": "half"})
        self.assertFalse(previous["inventory"])
        self.assertIsNone(result["error"])

    def test_previous_inventory_is_flagged(self):
        plan = json.loads((self.root / "计划" / "2026-10-22.json").read_text(encoding="utf-8"))
        plan["kind"] = "inventory"
        (self.root / "计划" / "2026-10-22.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(planner.view(self.root, today=TODAY)["previous"]["inventory"])
        self.assertEqual(planner.check_plan(self.root, "2026-10-22"), [])

    def test_calendar_summarises_every_plan_and_marks_the_inventory(self):
        plan = json.loads((self.root / "计划" / "2026-10-22.json").read_text(encoding="utf-8"))
        plan.update(kind="inventory", title="开学盘点")
        (self.root / "计划" / "2026-10-22.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        planner.set_state(self.root, "2026-10-22", "iti-l02", "done")
        planner.set_state(self.root, "2026-10-22", "ca-lec02", "half")
        entries = planner.view(self.root, today=TODAY)["calendar"]
        self.assertEqual([e["date"] for e in entries], ["2026-10-22", "2026-10-23"])
        inventory, day = entries
        self.assertEqual((inventory["inventory"], inventory["title"], inventory["tasks"], inventory["done"], inventory["half"]),
                         (True, "开学盘点", 3, 1, 1))
        self.assertEqual((day["inventory"], day["title"], day["tasks"], day["done"]), (False, None, 5, 0))

    def test_view_falls_back_to_latest_earlier_plan_and_explicit_dates(self):
        self.assertEqual(planner.view(self.root, today=date(2026, 10, 30))["date"], "2026-10-23")
        early = planner.view(self.root, today=date(2026, 10, 1))
        self.assertEqual(early["date"], "2026-10-22")  # nothing earlier: the first plan
        self.assertIsNone(planner.view(self.root, "2026-10-22", today=TODAY)["previous"])
        missing = planner.view(self.root, "2026-11-01", today=TODAY)
        self.assertIsNone(missing["plan"])
        with self.assertRaises(planner.PlanError):
            planner.view(self.root, "2026-13-01", today=TODAY)

    def test_missing_folder_is_explained_not_an_exception(self):
        result = planner.view(self.root / "nowhere", today=TODAY)
        self.assertIsNone(result["plan"])
        self.assertIn("还没有规划文件夹", result["error"])

    def test_ticks_are_written_cleared_and_kept_per_day(self):
        self.assertEqual(planner.set_state(self.root, "2026-10-23", "an-test1", "done"), {"an-test1": "done"})
        planner.set_state(self.root, "2026-10-23", "fop-practice", "half")
        self.assertEqual(planner.set_state(self.root, "2026-10-23", "an-test1", None), {"fop-practice": "half"})
        stored = json.loads(self.progress.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["days"]["2026-10-23"]["fop-practice"]["state"], "half")
        self.assertIn("at", stored["days"]["2026-10-23"]["fop-practice"])
        planner.set_state(self.root, "2026-10-23", "fop-practice", None)
        self.assertEqual(json.loads(self.progress.read_text(encoding="utf-8"))["days"], {})
        self.assertEqual([p.name for p in self.root.iterdir() if p.suffix == ".tmp"], [])

    def test_only_debt_tasks_can_be_dropped(self):
        self.assertEqual(planner.set_state(self.root, "2026-10-23", "ds-sheet2", "dropped"), {"ds-sheet2": "dropped"})
        with self.assertRaises(planner.PlanError):
            planner.set_state(self.root, "2026-10-23", "an-test1", "dropped")

    def test_bad_requests_change_nothing(self):
        cases = [("2026-10-23", "unknown-task", "done"), ("2026-10-24", "an-test1", "done"),
                 ("2026-10-23", "an-test1", "finished"), ("2026-10-23", "../x", "done"),
                 ("2026-10-23", 7, "done"), ("23.10.2026", "an-test1", "done"), ("2026-02-30", "an-test1", "done")]
        for day, task, state in cases:
            with self.subTest(day=day, task=task, state=state), self.assertRaises(planner.PlanError):
                planner.set_state(self.root, day, task, state)
        self.assertFalse(self.progress.exists())

    def test_damaged_progress_is_never_overwritten(self):
        self.progress.write_text("{not json", encoding="utf-8")
        with self.assertRaises(planner.PlanError):
            planner.set_state(self.root, "2026-10-23", "an-test1", "done")
        self.assertEqual(self.progress.read_text(encoding="utf-8"), "{not json")
        self.progress.write_text(json.dumps({"version": 2, "days": {}}), encoding="utf-8")
        with self.assertRaises(planner.PlanError):
            planner.view(self.root, today=TODAY)

    def test_invalid_plans_are_rejected(self):
        plans = self.root / planner.PLAN_DIR
        (plans / "2026-10-24.json").write_text(json.dumps({"tasks": [{"id": "a", "title": "x"},
                                                                     {"id": "a", "title": "y"}]}), encoding="utf-8")
        (plans / "notes.json").write_text("{}", encoding="utf-8")
        self.assertEqual(planner.plan_dates(self.root), ["2026-10-22", "2026-10-23", "2026-10-24"])
        with self.assertRaises(planner.PlanError):
            planner.load_plan(self.root, "2026-10-24")

    def test_status_of_the_last_run_is_passed_on(self):
        (self.root / planner.STATUS_FILE).write_text(json.dumps({"ok": False, "message": "Moodle 登录失败"}),
                                                    encoding="utf-8")
        self.assertEqual(planner.view(self.root, today=TODAY)["status"]["message"], "Moodle 登录失败")
        (self.root / planner.STATUS_FILE).write_text("broken", encoding="utf-8")
        self.assertFalse(planner.view(self.root, today=TODAY)["status"]["ok"])

    def test_debt_counts_required_work_before_the_running_day(self):
        planner.set_state(self.root, "2026-10-22", "ca-lec02", "half")
        owed = planner.debt(self.root, "2026-10-23")
        # Half of 45 minutes remains 22.5; internal seconds are never rounded to minutes.
        self.assertEqual([(i["key"], i["minutes_left"]) for i in owed["items"]],
                         [("2026-10-22/ca-lec02", 22.5), ("2026-10-22/iti-l02", 60)])
        self.assertEqual(owed["total_hours"], 1.38)
        self.assertEqual(owed["by_course"][0]["course_key"], "INHN0001")
        # The 10-23 plan is still running and does not count yet.
        self.assertEqual(planner.debt(self.root, "2026-10-22")["items"], [])

    def test_carried_copies_are_one_piece_of_work(self):
        plan = json.loads((self.root / "计划" / "2026-10-23.json").read_text(encoding="utf-8"))
        plan["tasks"].append({"id": "iti-l02-again", "title": "读 L02（补）", "section": "course", "course_key": "INHN0001",
                              "required": True, "origin": "debt", "carry_of": "2026-10-22/iti-l02", "minutes": 40})
        (self.root / "计划" / "2026-10-23.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        owed = {i["key"]: i["minutes_left"] for i in planner.debt(self.root, "2026-10-24")["items"]}
        self.assertEqual(owed["2026-10-22/iti-l02"], 60)  # rescheduling does not reduce the root budget
        planner.set_state(self.root, "2026-10-23", "iti-l02-again", "half")
        owed = {i["key"]: i["minutes_left"] for i in planner.debt(self.root, "2026-10-24")["items"]}
        self.assertEqual(owed["2026-10-22/iti-l02"], 40)  # half of the 40-minute prefix contributes 20
        planner.set_state(self.root, "2026-10-22", "iti-l02", "done")  # ticked late on the old day
        self.assertNotIn("2026-10-22/iti-l02", {i["key"] for i in planner.debt(self.root, "2026-10-24")["items"]})
        planner.set_state(self.root, "2026-10-23", "ds-sheet2", "dropped")
        self.assertNotIn("2026-10-21/ds-sheet2", {i["key"] for i in planner.debt(self.root, "2026-10-24")["items"]})

    def test_check_plan_reports_problems(self):
        self.assertEqual(planner.check_plan(self.root, "2026-10-22"), [])
        self.assertEqual(planner.check_plan(self.root, "2026-10-23"), [])
        plan = json.loads((self.root / "计划" / "2026-10-23.json").read_text(encoding="utf-8"))
        plan["tasks"][0].update(minutes="90", section="other")
        plan["tasks"][2].pop("carry_of")
        plan["upcoming"].append({"date": "soon", "title": "x"})
        (self.root / "计划" / "2026-10-23.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        problems = planner.check_plan(self.root, "2026-10-23")
        self.assertEqual(len(problems), 4, problems)
        self.assertEqual(planner.check_plan(self.root, "2026-12-01"), ["没有 2026-12-01 的计划文件。"])


if __name__ == "__main__":
    unittest.main()
