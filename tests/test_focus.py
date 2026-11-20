"""Offline tests for the pomodoro timer and focus statistics (focus.py) and their web routes.

Every timestamp is passed in explicitly, in a fixed UTC+2 zone, so the tests do not
depend on this computer's clock or time zone. Nothing touches the network or keyring.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import focus  # noqa: E402

TZ = timezone(timedelta(hours=2))


def at(text):
    """Local wall time in TZ, e.g. at("2026-10-23 09:00")."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def temp_dir(case):
    saved_mkdir = os.mkdir
    with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kwargs: saved_mkdir(path, 0o777, **kwargs)):
        return Path(case.enterContext(tempfile.TemporaryDirectory()))


class TimerTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def call(self, function, *args, when, **kwargs):
        return function(self.root, *args, now=at(when), tz=TZ, **kwargs)

    def stored(self):
        return json.loads((self.root / focus.FILE).read_text(encoding="utf-8"))

    def test_pauses_are_not_focus_time_and_a_late_look_records_the_real_end(self):
        task = {"course_key": "INHN0002", "course": "Fundamentals of Programming", "title": "H01E01",
                "plan_date": "2026-10-23", "plan_task": "fop-h01e01"}
        state = self.call(focus.start, "focus", task, when="2026-10-23 09:00")
        self.assertEqual(state["active"]["planned_seconds"], 25 * 60)
        self.call(focus.pause, when="2026-10-23 09:10")
        paused = self.call(focus.state, when="2026-10-23 09:40")
        self.assertEqual((paused["active"]["elapsed"], paused["active"]["paused"]), (600, True))
        self.call(focus.resume, when="2026-10-23 09:40")
        # Nobody looks until 11:00: the focus ended at 09:55, the 5-minute break at 10:00.
        later = self.call(focus.state, when="2026-10-23 11:00")
        self.assertEqual([e["type"] for e in later["events"]], ["focus_done", "break_done"])
        session = later["events"][0]["session"]
        self.assertEqual((session["focus_seconds"], session["completed"], session["end"]),
                         (1500, True, "2026-10-23T07:55:00+00:00"))
        self.assertIsNone(later["active"])
        self.assertEqual(later["today"]["pomodoros"], 1)
        self.assertEqual(later["today"]["focus_seconds"], 1500)
        self.assertEqual(self.call(focus.state, when="2026-10-23 11:01")["events"], [])  # recorded once only
        self.assertEqual(len(self.stored()["sessions"]), 1)
        self.assertEqual(focus.plan_seconds(self.root, "2026-10-23"), {"fop-h01e01": 1500})

    def test_break_starts_by_itself_and_every_fourth_is_long(self):
        when = at("2026-10-23 08:00")
        for number in range(1, 5):
            self.call(focus.start, "focus", when=when.isoformat())
            state = self.call(focus.state, when=(when + timedelta(minutes=26)).isoformat())
            expected = "long" if number == 4 else "short"
            self.assertEqual(state["active"]["mode"], expected)
            self.assertEqual(state["today"]["cycle"], number)
            self.call(focus.stop, when=(when + timedelta(minutes=27)).isoformat())  # skip the break
            when += timedelta(minutes=30)
        self.call(focus.update_settings, {"auto_break": False}, when=when.isoformat())
        self.call(focus.start, "focus", when=when.isoformat())
        state = self.call(focus.state, when=(when + timedelta(minutes=40)).isoformat())
        self.assertIsNone(state["active"])
        self.assertEqual(state["next_mode"], "short")

    def test_early_stop_keeps_time_but_not_the_pomodoro(self):
        self.call(focus.start, "focus", when="2026-10-23 09:00")
        short = self.call(focus.stop, "phone", when="2026-10-23 09:00:40")
        self.assertIsNone(short["saved"])  # under a minute: not recorded
        self.call(focus.start, "focus", when="2026-10-23 09:05")
        stopped = self.call(focus.stop, "phone", when="2026-10-23 09:17")
        self.assertEqual((stopped["saved"]["focus_seconds"], stopped["saved"]["completed"], stopped["saved"]["reason"]),
                         (720, False, "phone"))
        self.assertEqual((stopped["today"]["pomodoros"], stopped["next_mode"]), (0, "focus"))
        self.call(focus.start, "focus", when="2026-10-23 10:00")
        discarded = self.call(focus.stop, save=False, when="2026-10-23 10:20")
        self.assertIsNone(discarded["saved"])
        self.assertEqual(len(self.stored()["sessions"]), 1)
        with self.assertRaises(focus.FocusError):
            self.call(focus.stop, "bored", when="2026-10-23 10:30")

    def test_forgotten_stopwatch_stops_after_three_hours(self):
        self.call(focus.start, "stopwatch", {"course_key": "research", "title": "论文"}, when="2026-10-23 13:00")
        state = self.call(focus.state, when="2026-10-23 20:00")
        session = state["events"][0]["session"]
        self.assertEqual((session["kind"], session["focus_seconds"], session.get("auto_stopped")), ("stopwatch", 3 * 3600, True))
        self.assertIsNone(state["active"])
        self.assertEqual(state["today"]["pomodoros"], 0)

    def test_one_timer_at_a_time_and_inputs_are_checked(self):
        self.call(focus.start, "focus", when="2026-10-23 09:00")
        with self.assertRaises(focus.FocusError):
            self.call(focus.start, "short", when="2026-10-23 09:01")
        for bad in ({"course_key": "../x"}, {"plan_date": "2026-13-01", "plan_task": "a"}, {"title": "x" * 121}, "text"):
            with self.assertRaises(focus.FocusError):
                self.call(focus.set_task, bad, when="2026-10-23 09:02")
        for bad in ({"focus_minutes": 0}, {"focus_minutes": "25"}, {"sound": 1}, {"colour": True}, {}):
            with self.assertRaises(focus.FocusError):
                self.call(focus.update_settings, bad, when="2026-10-23 09:03")
        changed = self.call(focus.set_task, {"course_key": "INHN0011", "title": "ER"}, when="2026-10-23 09:04")
        self.assertEqual(changed["active"]["task"], {"course_key": "INHN0011", "title": "ER"})

    def test_a_damaged_file_is_never_overwritten(self):
        (self.root / focus.FILE).write_text("{not json", encoding="utf-8")
        with self.assertRaises(focus.FocusError):
            self.call(focus.start, "focus", when="2026-10-23 09:00")
        self.assertEqual((self.root / focus.FILE).read_text(encoding="utf-8"), "{not json")


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def call(self, function, *args, when="2026-10-23 22:00", **kwargs):
        return function(self.root, *args, now=at(when), tz=TZ, **kwargs)

    def test_manual_entries_are_marked_and_never_overlap(self):
        added = self.call(focus.add_manual, "2026-10-23T14:00", 50, {"course_key": "INHN0014", "title": "Kapitel 4"}, "图书馆")
        self.assertEqual((added["saved"]["kind"], added["saved"]["focus_seconds"], added["saved"]["completed"]),
                         ("manual", 3000, False))
        self.assertEqual(added["saved"]["start"], "2026-10-23T12:00:00+00:00")
        for start, minutes in (("2026-10-23T14:30", 30), ("2026-10-23T13:30", 31)):
            with self.assertRaises(focus.FocusError):
                self.call(focus.add_manual, start, minutes)
        with self.assertRaises(focus.FocusError):
            self.call(focus.add_manual, "2026-10-23T21:30", 60)  # ends in the future
        self.call(focus.add_manual, "2026-10-23T13:30", 30)  # touching is fine
        stats = focus.compute_stats(focus.load(self.root), "day", "2026-10-23", now=at("2026-10-23 22:00"), tz=TZ)
        self.assertEqual((stats["kpis"]["focus_seconds"], stats["kpis"]["pomodoros"], stats["kpis"]["sessions"]), (4800, 0, 0))

    def test_edit_changes_labels_only_and_delete_removes(self):
        self.call(focus.start, "focus", when="2026-10-23 09:00")
        saved = self.call(focus.stop, when="2026-10-23 09:10")["saved"]
        edited = self.call(focus.edit, saved["id"], {"task": {"course_key": "INHN0004", "title": "Sheet 1"},
                                                     "note": "太吵", "reason": "errand"})
        self.assertEqual((edited["saved"]["task"]["title"], edited["saved"]["note"], edited["saved"]["reason"]),
                         ("Sheet 1", "太吵", "errand"))
        with self.assertRaises(focus.FocusError):
            self.call(focus.edit, saved["id"], {"focus_seconds": 9999})
        self.call(focus.edit, saved["id"], {"reason": None, "note": None})
        self.assertNotIn("reason", focus.load(self.root)["sessions"][0])
        self.call(focus.delete, saved["id"])
        self.assertEqual(focus.load(self.root)["sessions"], [])
        with self.assertRaises(focus.FocusError):
            self.call(focus.delete, saved["id"])

    def test_import_adds_new_records_and_skips_known_or_overlapping(self):
        self.call(focus.add_manual, "2026-10-20T10:00", 60)
        backup = focus.export(self.root)
        other = temp_dir(self)
        focus.add_manual(other, "2026-10-21T10:00", 30, now=at("2026-10-23 22:00"), tz=TZ)
        focus.add_manual(other, "2026-10-20T10:30", 30, now=at("2026-10-23 22:00"), tz=TZ)  # overlaps the first
        payload = focus.export(other)
        payload["sessions"] += backup["sessions"] + [{"id": "zz", "kind": "manual"}]
        result = self.call(focus.import_records, payload)["imported"]
        self.assertEqual(result, {"added": 1, "skipped": 2, "invalid": 1})
        with self.assertRaises(focus.FocusError):
            self.call(focus.import_records, {"sessions": []})


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def focus_block(self, start, minutes, task=None, reason=None):
        begin = at(start)
        # An interrupted block is a default 25-minute pomodoro stopped early; otherwise it runs out.
        focus.start(self.root, "focus", task, minutes=None if reason else minutes, now=begin, tz=TZ)
        if reason:
            return focus.stop(self.root, reason, now=begin + timedelta(minutes=minutes), tz=TZ)
        return focus.stop(self.root, now=begin + timedelta(minutes=minutes, seconds=1), tz=TZ)

    def test_midnight_session_is_split_between_days_and_hours(self):
        focus.start(self.root, "stopwatch", {"course_key": "INHN0014"}, now=at("2026-10-22 23:50"), tz=TZ)
        focus.stop(self.root, now=at("2026-10-23 00:15"), tz=TZ)
        data = focus.load(self.root)
        now = at("2026-10-23 12:00")
        day1 = focus.compute_stats(data, "day", "2026-10-22", now=now, tz=TZ)
        day2 = focus.compute_stats(data, "day", "2026-10-23", now=now, tz=TZ)
        self.assertEqual((day1["kpis"]["focus_seconds"], day2["kpis"]["focus_seconds"]), (600, 900))
        self.assertEqual((day1["hours"][23], day2["hours"][0]), (600, 900))
        self.assertEqual(len(day1["records"]), 1)
        self.assertEqual(len(day2["records"]), 1)
        self.assertEqual(day1["kpis"]["sessions"], 0)  # counted on the day it ended
        self.assertEqual(day2["kpis"]["sessions"], 1)

    def test_week_kpis_courses_reasons_and_streaks(self):
        db = {"course_key": "INHN0011", "course": "Databases", "title": "ER"}
        an = {"course_key": "INHN0014", "course": "Analysis", "title": "Kapitel 4"}
        for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
            self.focus_block(day + " 09:00", 25, db)        # completed pomodoro (stops one second late)
        self.focus_block("2026-10-21 14:00", 10, an, reason="phone")
        self.focus_block("2026-10-23 15:00", 25, an)
        focus.add_manual(self.root, "2026-10-23T16:00", 60, an, now=at("2026-10-23 20:00"), tz=TZ)
        stats = focus.compute_stats(focus.load(self.root), "week", "2026-10-23", now=at("2026-10-23 20:00"), tz=TZ)
        k = stats["kpis"]
        self.assertEqual((stats["start"], stats["end"], len(stats["buckets"])), ("2026-10-19", "2026-10-25", 7))
        self.assertEqual(k["focus_seconds"], (4 * 25 + 10 + 60) * 60)
        self.assertEqual((k["pomodoros"], k["interrupted"], k["sessions"], k["completion_rate"]), (4, 1, 5, 0.8))
        self.assertEqual((k["active_days"], k["days"]), (4, 7))
        self.assertEqual(k["avg_active_day"], k["focus_seconds"] // 4)
        self.assertEqual(k["avg_calendar_day"], k["focus_seconds"] // 5)  # Monday … today
        self.assertEqual((k["best_day"], k["longest_seconds"]), ("2026-10-23", 3600))  # manual entries count too
        self.assertEqual([c["course_key"] for c in stats["by_course"]], ["INHN0014", "INHN0011"])
        self.assertEqual(stats["reasons"], {"phone": 1})
        self.assertEqual(stats["buckets"][0], {"key": "2026-10-19", "seconds": 1500, "pomodoros": 1})
        self.assertEqual(stats["hours"][9], 3 * 1500)
        o = stats["overall"]
        self.assertEqual((o["streak"], o["best_streak"], o["pomodoros"]), (1, 3, 4))  # 10/22 had no focus
        self.assertEqual(o["goal_streak"], 0)
        self.assertEqual(len(stats["heatmap"]["days"]) % 7 in range(7), True)
        self.assertEqual(stats["heatmap"]["days"][-1], 85 * 60)
        self.assertIsNone(stats["records"])

    def test_periods_previous_and_custom_range(self):
        self.focus_block("2026-09-30 10:00", 25)
        self.focus_block("2026-10-01 10:00", 25)
        now = at("2026-10-23 12:00")
        data = focus.load(self.root)
        month = focus.compute_stats(data, "month", "2026-10-05", now=now, tz=TZ)
        self.assertEqual((month["start"], month["end"], len(month["buckets"])), ("2026-10-01", "2026-10-31", 31))
        self.assertEqual((month["kpis"]["focus_seconds"], month["kpis"]["previous_seconds"]), (1500, 1500))
        year = focus.compute_stats(data, "year", now=now, tz=TZ)
        self.assertEqual([b["seconds"] for b in year["buckets"]][8:10], [1500, 1500])
        custom = focus.compute_stats(data, "custom", start="2026-09-30", end="2026-10-01", now=now, tz=TZ)
        self.assertEqual((custom["kpis"]["focus_seconds"], custom["kpis"]["avg_active_day"]), (3000, 1500))
        for bad in (dict(period="custom", start="2026-10-02", end="2026-10-01"), dict(period="decade")):
            with self.assertRaises(focus.FocusError):
                focus.compute_stats(data, now=now, tz=TZ, **bad)

    def test_history_filters_by_course_and_text(self):
        self.focus_block("2026-10-23 09:00", 25, {"course_key": "INHN0011", "title": "ER diagram"})
        self.focus_block("2026-10-23 10:00", 25, {"title": "邮件"})
        result = focus.history(self.root, course="other", now=at("2026-10-23 12:00"), tz=TZ)
        self.assertEqual([r["task"]["title"] for r in result["records"]], ["邮件"])
        result = focus.history(self.root, query="er DIAG", now=at("2026-10-23 12:00"), tz=TZ)
        self.assertEqual((len(result["records"]), result["total_seconds"]), (1, 1500))
        with self.assertRaises(focus.FocusError):
            focus.history(self.root, course="../x", now=at("2026-10-23 12:00"), tz=TZ)


SPEC = importlib.util.spec_from_file_location("focus_web_fixture", Path(__file__).with_name("test_web.py"))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class FocusWebTest(unittest.TestCase):
    """The same app and fake tools as test_web.py (without re-running its tests); focus routes never call a tool."""

    setUp, get, post = fixture.WebTest.setUp, fixture.WebTest.get, fixture.WebTest.post

    def test_timer_routes_and_records(self):
        status, state = self.get("/api/focus")
        self.assertEqual((status, state["active"], state["settings"]["focus_minutes"]), (200, None, 25))
        task = {"course_key": "INHN0014", "title": "Test", "plan_date": "2026-10-23", "plan_task": "an-test1"}
        status, started = self.post("/api/focus/action", {"action": "start", "mode": "focus", "task": task})
        self.assertEqual((status, started["active"]["mode"]), (200, "focus"))
        self.assertEqual(self.post("/api/focus/action", {"action": "start", "mode": "focus"})[0], 400)
        self.assertEqual(self.post("/api/focus/action", {"action": "explode"})[0], 400)
        self.assertEqual(self.post("/api/focus/action", {"action": "pause"})[1]["active"]["paused"], True)
        self.assertEqual(self.post("/api/focus/action", {"action": "discard"})[1]["active"], None)
        self.assertEqual(self.post("/api/focus/settings", {"changes": {"goal_minutes": 300}})[1]["settings"]["goal_minutes"], 300)
        status, added = self.post("/api/focus/records", {"start": "2026-10-01T10:00", "minutes": 40, "task": task})
        self.assertEqual((status, added["saved"]["kind"]), (200, "manual"))
        record = added["saved"]["id"]
        self.assertEqual(self.get("/api/focus/stats?period=custom&start=2026-10-01&end=2026-10-01")[1]["kpis"]["focus_seconds"], 2400)
        self.assertEqual(self.get("/api/focus/stats?period=week&date=2026-02-30")[0], 400)
        self.assertEqual(len(self.get("/api/focus/history?start=2026-10-01&end=2026-10-02")[1]["records"]), 1)
        self.assertEqual(self.post("/api/focus/records/edit", {"id": record, "changes": {"note": "补"}})[1]["saved"]["note"], "补")
        plan = self.get("/api/plan?date=2026-10-23")[1]
        self.assertEqual(plan["plan"]["tasks"][0]["focus_seconds"], 2400)  # the plan page shows time spent
        backup = self.get("/api/focus/export")[1]
        self.assertEqual(len(backup["sessions"]), 1)
        self.assertEqual(self.post("/api/focus/records/delete", {"id": record})[0], 400)  # needs confirmation
        self.assertEqual(self.post("/api/focus/records/delete", {"id": record, "confirm": True})[0], 200)
        self.assertEqual(self.post("/api/focus/import", {"data": backup})[0], 400)
        self.assertEqual(self.post("/api/focus/import", {"data": backup, "confirm": True})[1]["imported"]["added"], 1)
        self.assertTrue((self.planner_dir / focus.FILE).exists())
        self.assertEqual(self.calls, [])  # no tool, no login

    def test_focus_routes_need_the_token(self):
        self.assertEqual(fixture.request(self.app, "GET", "/api/focus", headers={"host": f"127.0.0.1:{fixture.PORT}"})[0], 403)


if __name__ == "__main__":
    unittest.main()
