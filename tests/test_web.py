"""Offline tests for the local web UI. Run with the project's Python and -I.

The ASGI app is driven directly (no httpx, no server, no browser); the tools are
fakes that record their arguments, so nothing touches Moodle, Artemis or keyring.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("moodle_web", ROOT / "moodle_web.py")
web = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = web
SPEC.loader.exec_module(web)

PORT = 48123
TOKEN = "unit-test-token-0123456789"
GOOD = {"host": f"127.0.0.1:{PORT}", "x-ui-token": TOKEN}
TOOL_NAMES = ("list_courses", "list_activities", "list_files", "read_page", "download",
              "upcoming_deadlines", "grades", "assignments", "sync_course",
              "artemis_courses", "artemis_lectures", "artemis_download", "artemis_sync",
              "artemis_exercise_repos")


def request(app, method, path, body=None, headers=GOOD):
    path, _, query = path.partition("?")
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT),
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]
                        + [(b"content-type", b"application/json")]}
    messages = []

    async def run():
        # Like a real server: the body once, then http.disconnect only after the response is complete
        # (newer Starlette file responses listen for the disconnect while they send).
        done = asyncio.Event()
        calls = [0]

        async def receive():
            calls[0] += 1
            if calls[0] == 1:
                return {"type": "http.request", "body": data, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                done.set()

        await app(scope, receive, send)

    asyncio.run(run())
    start = messages[0]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


class WebTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.results = {}

        def fake(name):
            def tool(**kwargs):
                self.calls.append((name, kwargs))
                return self.results.get(name, {"tool": name})
            return tool

        self.tools = {name: fake(name) for name in TOOL_NAMES}
        self.opened = []
        saved_mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kwargs:
                   saved_mkdir(path, 0o777, **kwargs)):
            self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.map_path = self.tmp / "课程对照表.md"
        self.map_path.write_text((FIXTURES / "course_map.md").read_text(encoding="utf-8"), encoding="utf-8")
        self.planner_dir = self.tmp / "Planner"
        shutil.copytree(FIXTURES / "planner", self.planner_dir)
        self.app = web.create_app(self.tools, token=TOKEN, port=PORT, map_path=self.map_path,
                                  opener=self.opened.append, planner_dir=self.planner_dir)

    def get(self, path, headers=GOOD):
        status, _, body = request(self.app, "GET", path, headers=headers)
        return status, json.loads(body)

    def post(self, path, body, headers=GOOD):
        status, _, raw = request(self.app, "POST", path, body, headers=headers)
        return status, json.loads(raw)

    # -- access control ------------------------------------------------------

    def test_plan_enrichment_is_read_only_and_keeps_zero_pass_distinct_from_framework(self):
        folder = self.tmp / 'practice'
        folder.mkdir()
        practice = {'folder': str(folder), 'tests_total': 9}
        records = {'version': 1, 'problems': [{'folder': str(folder), 'skeleton_sha256': 'framework',
                   'last_result': {'code_sha256': 'attempt', 'passed': 0, 'total': 9, 'checked_at': '2026-11-06T12:00:00+01:00'}}]}
        path = self.planner_dir / '练习记录.json'
        path.write_text(json.dumps(records), encoding='utf-8')
        api = web.Api(self.tools, self.map_path, self.opened.append, self.planner_dir)
        def enrich():
            return api.enrich_plan({'date': '2026-10-23', 'summary': {}, 'plan': {'tasks': [{'id': 'a', 'practice': dict(practice)}]},
                                    'previous': {'date': '2026-10-22', 'tasks': [{'id': 'b', 'practice': dict(practice)}]}})
        before = {str(p): p.read_bytes() for p in self.planner_dir.rglob('*') if p.is_file()}
        with patch.object(web.focus, 'stats', side_effect=AssertionError('plan must not settle/write timers')):
            result = enrich()
        self.assertEqual(result['plan']['tasks'][0]['practice']['attempt_state'], 'attempted')
        self.assertEqual(result['plan']['tasks'][0]['practice']['tests_passed'], 0)
        self.assertEqual(result['previous']['tasks'][0]['practice']['attempt_state'], 'attempted')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.planner_dir.rglob('*') if p.is_file()})
        self.assertEqual(self.calls, [])
        records['problems'][0]['last_result']['code_sha256'] = 'framework'
        path.write_text(json.dumps(records), encoding='utf-8')
        self.assertEqual(enrich()['plan']['tasks'][0]['practice']['attempt_state'], 'untouched')
        records['problems'][0].pop('skeleton_sha256')
        path.write_text(json.dumps(records), encoding='utf-8')
        self.assertEqual(enrich()['plan']['tasks'][0]['practice']['attempt_state'], 'unknown')
        path.unlink()
        self.assertEqual(enrich()['plan']['tasks'][0]['practice']['attempt_state'], 'unknown')

    def test_plan_reports_missing_material_even_without_practice_record_file(self):
        api = web.Api(self.tools, self.map_path, self.opened.append, self.planner_dir)
        result = api.enrich_plan({'plan': {'tasks': [{'id': 'a', 'materials': [{'path': str(self.tmp / 'missing.pdf')}]}]}})
        self.assertTrue(result['plan']['tasks'][0]['materials'][0]['unavailable'])

    def test_api_requires_token_host_and_same_origin(self):
        bad = [
            {"host": f"127.0.0.1:{PORT}"},
            {"host": f"127.0.0.1:{PORT}", "x-ui-token": TOKEN + "x"},
            {"host": f"evil.example:{PORT}", "x-ui-token": TOKEN},
            {"host": "127.0.0.1:1", "x-ui-token": TOKEN},
            {**GOOD, "origin": "https://evil.example"},
            {**GOOD, "origin": "null"},
            {**GOOD, "sec-fetch-site": "cross-site"},
        ]
        for headers in bad:
            with self.subTest(headers=headers):
                status, body = self.get("/api/deadlines", headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(TOKEN, json.dumps(body))
        self.assertEqual(self.calls, [])
        ok = {**GOOD, "origin": f"http://127.0.0.1:{PORT}", "sec-fetch-site": "same-origin"}
        self.assertEqual(self.get("/api/deadlines", headers=ok)[0], 200)

    def test_tailnet_requests_need_the_users_own_login_instead_of_the_token(self):
        tailnet = web.Tailnet(frozenset({"pc.tail0.ts.net", "pc"}), "me@example.org")
        app = web.create_app(self.tools, token=TOKEN, port=PORT, map_path=self.map_path,
                             opener=self.opened.append, planner_dir=self.planner_dir, tailnet=tailnet)
        mine = {"host": "pc.tail0.ts.net", "tailscale-user-login": "me@example.org",
                "x-ui-token": "tailnet", "origin": "http://pc.tail0.ts.net", "sec-fetch-site": "same-origin"}
        bad = [
            {**mine, "tailscale-user-login": "someone@example.org"},
            {k: v for k, v in mine.items() if k != "tailscale-user-login"},
            {**mine, "host": "evil.example"},
            {**mine, "origin": "https://evil.example"},
            {**mine, "origin": f"http://127.0.0.1:{PORT}"},
            {**mine, "sec-fetch-site": "cross-site"},
        ]
        for headers in bad:
            with self.subTest(headers=headers):
                self.assertEqual(request(app, "GET", "/api/deadlines", headers=headers)[0], 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(request(app, "GET", "/api/deadlines", headers=mine)[0], 200)
        self.assertEqual(request(app, "GET", "/api/deadlines", headers={**mine, "host": "pc"})[0], 200)
        self.assertEqual(request(app, "GET", "/", headers={"host": "pc.tail0.ts.net"})[0], 403)
        self.assertEqual(request(app, "GET", "/", headers={"host": "pc.tail0.ts.net",
                                                           "tailscale-user-login": "me@example.org"})[0], 200)
        # Folders would open on this screen, not on the device the page is on.
        folder = self.tmp / "INHN0001"
        folder.mkdir()
        status, _, _ = request(app, "POST", "/api/open-folder", {"path": str(folder)}, headers=mine)
        self.assertEqual((status, self.opened), (400, []))
        # Without a tailnet, the same headers are just a foreign host.
        self.assertEqual(self.get("/api/deadlines", headers=mine)[0], 403)

    def test_static_needs_local_host_and_sets_security_headers(self):
        status, headers, body = request(self.app, "GET", "/", headers={"host": f"127.0.0.1:{PORT}"})
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(request(self.app, "GET", "/", headers={"host": "rebind.example"})[0], 403)

    # -- routing and validation ----------------------------------------------

    def test_read_routes_pass_arguments(self):
        self.assertEqual(self.get("/api/deadlines?days=7"), (200, {"tool": "upcoming_deadlines"}))
        self.get("/api/courses/900001/activities")
        self.get("/api/courses/900001/assignments")
        self.get("/api/grades")
        self.get("/api/grades?course=900001")
        self.get("/api/artemis/courses")
        self.get("/api/artemis/901/lectures")
        self.post("/api/files", {"url": "/mod/resource/view.php?id=1"})
        self.post("/api/page", {"url": "/mod/page/view.php?id=2"})
        self.assertEqual(self.calls, [
            ("upcoming_deadlines", {"days": 7}),
            ("list_activities", {"course": "900001"}),
            ("assignments", {"course": "900001"}),
            ("grades", {"course": ""}),
            ("grades", {"course": "900001"}),
            ("artemis_courses", {}),
            ("artemis_lectures", {"course": "901"}),
            ("list_files", {"url": "/mod/resource/view.php?id=1"}),
            ("read_page", {"url": "/mod/page/view.php?id=2"}),
        ])

    def test_bad_input_is_rejected_before_any_tool(self):
        cases = [
            ("GET", "/api/deadlines?days=abc", None), ("GET", "/api/deadlines?days=0", None),
            ("GET", "/api/courses?which=everything", None), ("GET", "/api/grades?course=x", None),
            ("POST", "/api/files", {}), ("POST", "/api/page", b"not json"), ("POST", "/api/page", [1]),
            ("POST", "/api/sync", {"source": "dropbox", "course": 1, "dest_dir": str(self.tmp)}),
            ("POST", "/api/sync", {"source": "moodle", "course": "1", "dest_dir": str(self.tmp)}),
            ("POST", "/api/sync", {"source": "moodle", "course": True, "dest_dir": str(self.tmp)}),
            ("POST", "/api/sync", {"source": "moodle", "course": 1, "dest_dir": "relative\\dir"}),
            ("POST", "/api/sync", {"source": "moodle", "course": 1, "dest_dir": str(self.tmp), "dry_run": "no"}),
        ]
        for method, path, body in cases:
            with self.subTest(path=path, body=body):
                status, _, raw = request(self.app, method, path, body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(raw))
        self.assertEqual(self.calls, [])

    def test_writes_need_confirmation(self):
        dest = str(self.tmp)
        status, _ = self.post("/api/sync", {"source": "moodle", "course": 900001, "dest_dir": dest,
                                            "dry_run": False})
        self.assertEqual(status, 400)
        status, _ = self.post("/api/download", {"url": "/pluginfile.php/1/a.pdf", "dest_dir": dest})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

        self.post("/api/sync", {"source": "moodle", "course": 900001, "dest_dir": dest})
        self.post("/api/sync", {"source": "artemis", "course": 901, "dest_dir": dest,
                                "dry_run": False, "confirm": True})
        self.post("/api/download", {"url": "/pluginfile.php/1/a.pdf", "dest_dir": dest, "confirm": True})
        artemis_url = "https://artemis.tum.de/api/core/files/attachments/x/student/a.pdf"
        self.post("/api/download", {"url": artemis_url, "dest_dir": dest, "confirm": True})
        self.assertEqual(self.calls, [
            ("sync_course", {"course": "900001", "dest_dir": dest, "dry_run": True}),
            ("artemis_sync", {"course": "901", "dest_dir": dest, "dry_run": False}),
            ("download", {"url": "/pluginfile.php/1/a.pdf", "dest_dir": dest}),
            ("artemis_download", {"url": artemis_url, "dest_dir": dest}),
        ])

    # -- study plan ----------------------------------------------------------

    def test_plan_and_ticks_use_only_the_planner_folder(self):
        status, data = self.get("/api/plan?date=2026-10-23")
        self.assertEqual(status, 200)
        self.assertEqual(data["date"], "2026-10-23")
        self.assertEqual(data["plan"]["tasks"][0]["id"], "an-test1")
        self.assertEqual(data["previous"]["date"], "2026-10-22")
        self.assertEqual(self.get("/api/plan")[0], 200)
        status, updated = self.post("/api/progress", {"date": "2026-10-23", "task": "an-test1", "state": "done"})
        self.assertEqual((status, updated["date"], updated["states"]), (200, "2026-10-23", {"an-test1": "done"}))
        self.assertIn("derived", updated)
        self.assertEqual(self.post("/api/progress", {"date": "2026-10-23", "task": "an-test1", "state": None})[1]["states"], {})
        stored = json.loads((self.planner_dir / "进度.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["days"], {})
        self.assertEqual(self.calls, [])  # no tool, no login

    def test_optional_feedback_survives_state_changes_and_is_validated(self):
        body = {"date": "2026-10-23", "task": "an-test1", "state": "half",
                "actual_minutes": 75, "understanding": "hard"}
        status, data = self.post("/api/progress", body)
        self.assertEqual(status, 200)
        self.assertEqual(data["feedback"]["an-test1"]["actual_minutes"], 75)
        self.assertEqual(data["feedback"]["an-test1"]["understanding"], "hard")
        self.post("/api/progress", {"date": "2026-10-23", "task": "an-test1", "state": "done"})
        self.assertEqual(self.get("/api/plan?date=2026-10-23")[1]["feedback"]["an-test1"]["actual_minutes"], 75)
        for value in (0, -1, True, 1441, "75"):
            bad = dict(body, actual_minutes=value)
            self.assertEqual(self.post("/api/progress", bad)[0], 400)
        self.assertEqual(self.calls, [])

    def test_latest_practice_result_is_joined_without_running_java(self):
        plan = json.loads((self.planner_dir / "计划" / "2026-10-23.json").read_text(encoding="utf-8"))
        task = next(t for t in plan["tasks"] if t.get("practice"))
        records = {"version": 1, "problems": [{"folder": task["practice"]["folder"],
                   "last_result": {"total": 10, "passed": 7, "checked_at": "2030-01-01T12:00:00+01:00"}}]}
        (self.planner_dir / "练习记录.json").write_text(json.dumps(records), encoding="utf-8")
        result = self.get("/api/plan?date=2026-10-23")[1]
        practice = next(t["practice"] for t in result["plan"]["tasks"] if t.get("practice"))
        self.assertEqual((practice["tests_passed"], practice["tests_total"]), (7, 10))
        self.assertEqual(self.calls, [])

    def test_plan_requests_are_validated(self):
        self.assertEqual(self.get("/api/plan?date=2026-99-01")[0], 400)
        for body in ({"date": "2026-10-23", "task": "nope", "state": "done"},
                     {"date": "2026-10-23", "task": "an-test1", "state": "dropped"},
                     {"date": "2026-10-23", "task": "an-test1", "state": "maybe"},
                     {"task": "an-test1", "state": "done"}):
            with self.subTest(body=body):
                status, data = self.post("/api/progress", body)
                self.assertEqual(status, 400)
                self.assertIn("error", data)
        self.assertFalse((self.planner_dir / "进度.json").exists())
        self.assertEqual(self.get("/api/plan", headers={"host": f"127.0.0.1:{PORT}"})[0], 403)

    def test_artemis_exercise_ticks_are_the_users_own_and_stay_in_the_planner_folder(self):
        self.results["artemis_exercise_repos"] = [
            {"id": 5101, "title": "H01", "repositories": {"practice": None, "graded": None}},
            {"id": 5102, "title": "Quiz", "repositories": {"practice": None, "graded": None}}]
        status, data = self.get("/api/artemis/901/exercises")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("artemis_exercise_repos", {"course": "901"})])
        self.assertEqual([e["checked"] for e in data["exercises"]], [False, False])
        self.assertIsNone(data["checks_error"])

        self.calls.clear()
        status, data = self.post("/api/artemis/check", {"course": 901, "exercise": 5101, "done": True, "title": "H01"})
        self.assertEqual(status, 200)
        self.assertEqual(list(data["checked"]), ["5101"])
        self.assertEqual(self.calls, [])  # ticking never calls Artemis
        stored = json.loads((self.planner_dir / "Artemis练习.json").read_text(encoding="utf-8"))
        self.assertEqual(list(stored["checks"]), ["901:5101"])
        self.assertEqual(stored["checks"]["901:5101"]["title"], "H01")
        _, data = self.get("/api/artemis/901/exercises")
        self.assertEqual([e["checked"] for e in data["exercises"]], [True, False])
        _, data = self.get("/api/artemis/626/exercises")  # other courses keep their own ticks
        self.assertEqual([e["checked"] for e in data["exercises"]], [False, False])

        self.assertEqual(self.post("/api/artemis/check", {"course": 901, "exercise": 5101, "done": False})[1]["checked"], {})
        stored = json.loads((self.planner_dir / "Artemis练习.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["checks"], {})

        for body in ({"course": "901", "exercise": 5101, "done": True}, {"course": 901, "exercise": 0, "done": True},
                     {"course": 901, "exercise": 5101, "done": "yes"}, {"course": 901, "exercise": True, "done": True},
                     {"course": 901, "exercise": 5101, "done": True, "title": "x" * 201}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/artemis/check", body)[0], 400)

    def test_damaged_artemis_ticks_are_never_overwritten(self):
        path = self.planner_dir / "Artemis练习.json"
        path.write_text("{broken", encoding="utf-8")
        self.results["artemis_exercise_repos"] = [{"id": 5101, "title": "H01"}]
        status, data = self.get("/api/artemis/901/exercises")
        self.assertEqual(status, 200)
        self.assertTrue(data["checks_error"])
        self.assertEqual(self.post("/api/artemis/check", {"course": 901, "exercise": 5101, "done": True})[0], 400)
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_tool_errors_pass_through_unchanged(self):
        self.results["upcoming_deadlines"] = {"error": "登录失败：***"}
        self.assertEqual(self.get("/api/deadlines"), (200, {"error": "登录失败：***"}))

    # -- course map ----------------------------------------------------------

    def test_courses_are_linked_to_local_folders(self):
        self.results["list_courses"] = [{"id": 900001, "name": "Imaginary"}, {"id": 5, "name": "Other"}]
        status, courses = self.get("/api/courses?which=all")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [("list_courses", {"which": "all"})])
        self.assertEqual(courses[0]["local"]["artemis_id"], 901)
        self.assertIsNone(courses[1]["local"])

    def test_completed_courses_move_from_running_to_finished(self):
        # IMG0002 (900002) is marked 已完成 in the map; Moodle still lists it as in progress.
        self.results["list_courses"] = [{"id": 900001, "name": "Imaginary"}, {"id": 900002, "name": "Physics"}]
        status, running = self.get("/api/courses?which=inprogress")
        self.assertEqual([c["id"] for c in running], [900001])
        self.calls.clear()
        self.tools["list_courses"] = lambda which: (self.calls.append(("list_courses", {"which": which}))
            or ([{"id": 7, "name": "Old"}] if which == "past" else [{"id": 900001}, {"id": 900002, "name": "Physics"}]))
        status, finished = self.get("/api/courses?which=past")
        self.assertEqual([c["id"] for c in finished], [7, 900002])
        self.assertTrue(finished[1]["local"]["completed"])
        self.assertEqual(self.calls, [("list_courses", {"which": "past"}), ("list_courses", {"which": "inprogress"})])

    def test_map_reports_targets_and_missing_file(self):
        target = Path(self.tmp, "Semester 1 (WS 2030-31)", "IMG0002 Pretend Physics", "Official")
        target.mkdir(parents=True)
        status, data = self.get("/api/map")
        self.assertEqual(status, 200)
        self.assertIsNone(data["error"])
        self.assertEqual(data["inbox"], str(self.tmp / "_Inbox"))
        by_id = {e["moodle_id"]: e for e in data["entries"]}
        self.assertTrue(by_id[900002]["moodle_dest_exists"])
        self.assertFalse(by_id[900001]["moodle_dest_exists"])
        self.assertFalse(by_id[900002]["artemis_dest_exists"])
        self.map_path.unlink()
        status, data = self.get("/api/map")
        self.assertEqual((status, data["entries"]), (200, []))
        self.assertIn("没有找到课程对照表", data["error"])

    # -- open folder ---------------------------------------------------------

    def test_open_folder_only_existing_folders_under_root(self):
        folder = self.tmp / "Course"
        folder.mkdir()
        (folder / "run.bat").write_text("echo", encoding="utf-8")
        for path in (folder / "run.bat", self.tmp / "missing", self.tmp.parent, folder / ".." / ".."):
            with self.subTest(path=path):
                self.assertEqual(self.post("/api/open-folder", {"path": str(path)})[0], 400)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.post("/api/open-folder", {"path": str(folder)}), (200, {"ok": True}))
        self.assertEqual(self.opened, [str(folder.resolve())])

    # -- concurrency ---------------------------------------------------------

    def test_tool_calls_run_one_at_a_time(self):
        active, peak = [0], [0]
        guard = threading.Lock()

        def slow(**kwargs):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with guard:
                active[0] -= 1
            return []

        self.tools["upcoming_deadlines"] = slow
        api = self.app.app.state.api

        async def both():
            await asyncio.gather(api.call("upcoming_deadlines"), api.call("upcoming_deadlines"),
                                 api.call("upcoming_deadlines"))

        asyncio.run(both())
        self.assertEqual(peak[0], 1)

    # -- app mode: window presence ------------------------------------------

    def alive(self, headers=GOOD):
        """Open /api/alive, check the presence while it is held, then disconnect."""
        presence = self.app.app.state.presence
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/api/alive", "raw_path": b"/api/alive", "query_string": b"",
                 "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT),
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        messages, seen = [], []

        async def run():
            gone = asyncio.Event()
            calls = [0]

            async def receive():
                calls[0] += 1
                if calls[0] == 1:
                    return {"type": "http.request", "body": b"", "more_body": False}
                await gone.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            task = asyncio.create_task(self.app(scope, receive, send))
            await asyncio.sleep(0.05)
            seen.append((presence.open, presence.idle_for()))
            gone.set()
            await task

        asyncio.run(run())
        return messages[0]["status"], seen[0], (presence.open, presence.idle_for())

    def test_alive_counts_open_pages_until_they_disconnect(self):
        presence = self.app.app.state.presence
        self.assertEqual((presence.open, presence.ever, presence.idle_for()), (0, False, None))
        status, during, after = self.alive()
        self.assertEqual(status, 200)
        self.assertEqual(during, (1, None))
        self.assertEqual(after[0], 0)
        self.assertGreaterEqual(after[1], 0)
        self.assertTrue(presence.ever)

    def test_alive_needs_the_token(self):
        status, during, after = self.alive(headers={"host": f"127.0.0.1:{PORT}"})
        self.assertEqual(status, 403)
        self.assertFalse(self.app.app.state.presence.ever)

    # -- updates without closing the tabs ----------------------------------

    def test_version_changes_when_page_files_change(self):
        web_dir = self.tmp / "web"
        web_dir.mkdir()
        (web_dir / "app.js").write_text("1", encoding="utf-8")
        app = web.create_app(self.tools, token=TOKEN, port=PORT, map_path=self.tmp / "map.md",
                             web_dir=web_dir, planner_dir=self.tmp / "planner")
        status, _, body = request(app, "GET", "/api/version")
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertEqual(first["boot"], web.BOOT_ID)
        (web_dir / "app.js").write_text("22", encoding="utf-8")
        self.assertNotEqual(json.loads(request(app, "GET", "/api/version")[2])["web"], first["web"])
        self.assertEqual(request(app, "GET", "/api/version", headers={"host": f"127.0.0.1:{PORT}"})[0], 403)

    def test_broken_module_finds_a_half_saved_file(self):
        (self.tmp / "good.py").write_text("x = 1\n", encoding="utf-8")
        self.assertIsNone(web.broken_module(self.tmp))
        (self.tmp / "bad.py").write_text("def f(:\n", encoding="utf-8")
        self.assertEqual(web.broken_module(self.tmp), "bad.py")

    def test_resume_is_taken_once_and_checked(self):
        with patch.dict(os.environ, {web.RESUME_ENV: f"{PORT}:{TOKEN}"}):
            self.assertEqual(web.take_resume(), (PORT, TOKEN))
            self.assertNotIn(web.RESUME_ENV, os.environ)  # never inherited by Chrome or later children
            self.assertIsNone(web.take_resume())
        for bad in ("abc:" + TOKEN, "0:" + TOKEN, "70000:" + TOKEN, f"{PORT}:short", f"{PORT}:bad token!" * 3):
            with patch.dict(os.environ, {web.RESUME_ENV: bad}):
                self.assertIsNone(web.take_resume(), bad)

    def test_shortcut_opens_a_normal_chrome_tab(self):
        import app_window
        # The user's own Chrome and profile, like any other tab: no app window, no extra profile.
        self.assertEqual(app_window.browser_args(r"C:\Chrome\chrome.exe", "http://127.0.0.1:1/#t=x"),
                         [r"C:\Chrome\chrome.exe", "http://127.0.0.1:1/#t=x"])


if __name__ == "__main__":
    unittest.main()
