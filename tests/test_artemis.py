"""Offline Artemis tests. Run with the project's Python and -I.

All HTTP and credential access is blocked; fixtures, cookies and passwords are synthetic.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().parents[1] / "tum_moodle_auto.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "artemis"
SPEC = importlib.util.spec_from_file_location("tum_moodle_auto", SCRIPT)
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)
artemis = app.artemis

BASE = artemis.ARTEMIS_URL
JWT = "synthetic-jwt-cookie-value-0123456789"
PASSWORD = "synthetic-password"
SLIDES_2 = (BASE + "/api/core/files/attachments/attachment-video-units/7002/student/"
            "AttachmentUnit_2026-10-20T10-00-00-000_lec02.pdf")


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def response(url, status=200, body=b"", headers=None):
    result = app.requests.Response()
    result.url = url
    result.status_code = status
    result.headers.update(headers or {})
    result._content = body
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


def json_response(url, data):
    return response(url, body=json.dumps(data).encode(), headers={"Content-Type": "application/json"})


class ArtemisTestCase(unittest.TestCase):
    def setUp(self):
        self.blocked = [
            self.enterContext(patch("requests.adapters.HTTPAdapter.send",
                                    side_effect=AssertionError("HTTP is forbidden in offline tests"))),
            self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network"))),
            self.enterContext(patch("socket.socket.connect_ex", side_effect=AssertionError("No network"))),
            self.enterContext(patch.object(app.keyring, "get_password", side_effect=AssertionError("No keyring"))),
            self.enterContext(patch.object(app.keyring, "set_password", side_effect=AssertionError("No keyring"))),
        ]
        self.logins = 0
        self.client = artemis.TumArtemis(lambda: ("ab12cde", PASSWORD, None))
        self.addCleanup(self.client.session.close)
        self.owner = app.TumMcpServer()
        self.owner._artemis = self.client
        self.server = self.owner.build_server()
        self.routes = {}
        self.calls = []
        self.enterContext(patch.object(self.client.session, "post", side_effect=self.fake_post))
        self.enterContext(patch.object(self.client.session, "get", side_effect=self.fake_get))
        self.enterContext(patch.object(self.client.session, "head", side_effect=self.fake_head))
        self.login_status = 200

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()

    def fake_post(self, url, **kwargs):
        self.assertEqual(url, BASE + artemis.LOGIN_PATH)
        self.assertEqual(kwargs["json"]["password"], PASSWORD)
        self.logins += 1
        if self.login_status == 200:
            self.client.session.cookies.set("jwt", JWT, domain=artemis.ARTEMIS_HOST)
            return json_response(url, {"access_token": JWT})
        return response(url, status=self.login_status)

    def fake_get(self, url, **kwargs):
        self.calls.append(("GET", url))
        route = self.routes.get(url)
        if route is None:
            return response(url, status=404)
        return route(url) if callable(route) else route

    def fake_head(self, url, **kwargs):
        self.calls.append(("HEAD", url))
        route = self.routes.get(url)
        result = route(url) if callable(route) else route
        return response(url, headers=dict(result.headers)) if result is not None else response(url, status=404)

    def call_tool(self, name, **arguments):
        return self.server._tool_manager.get_tool(name).fn(**arguments)

    def serve_course(self, lectures=None):
        self.routes[BASE + "/api/course/courses/for-dashboard"] = json_response("", fixture("dashboard.json"))
        self.routes[BASE + "/api/lecture/courses/901/lectures-with-slides"] = json_response(
            "", fixture("lectures.json") if lectures is None else lectures)

    def temporary_directory(self):
        # Inherit the private system-temp ACL during creation only (see test_offline.py).
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw:
                   mkdir(path, 0o777, **kw)):
            return Path(self.enterContext(tempfile.TemporaryDirectory(prefix="artemis-test-")))


class ParserTests(unittest.TestCase):
    def test_courses_from_dashboard_skip_entries_without_id(self):
        courses = artemis.parse_courses(fixture("dashboard.json"))
        self.assertEqual([c["id"] for c in courses], [901, 902])
        self.assertEqual(courses[0]["short_name"], "IMALG")
        self.assertEqual(courses[0]["title"], "Imaginary Algorithms")
        self.assertEqual(courses[1]["start_date"], None)
        with self.assertRaises(app.MoodleError):
            artemis.parse_courses({"courses": "unexpected"})

    def test_lectures_sorted_by_date_with_student_urls(self):
        lectures = artemis.parse_lectures(fixture("lectures.json"), 901)
        self.assertEqual([l["id"] for l in lectures], [5001, 5002, 5003])
        first = lectures[0]["attachments"]
        self.assertEqual([a["unit_id"] for a in first], [7001, 7004, 7005])
        self.assertEqual(first[0]["filename"], "Slides 1.pdf")
        self.assertTrue(first[0]["url"].endswith("/attachment-video-units/7001/student/"
                                                 "AttachmentUnit_2026-10-13T10-00-00-000_lec01.pdf"))
        # Lecture-level attachments have no student variant; the name keeps one extension.
        self.assertEqual(first[1]["url"], BASE + "/api/core/files/attachments/lecture/5001/Exercise_sheet_1.zip")
        self.assertEqual(first[1]["filename"], "Exercise sheet 1.zip")
        self.assertIsNone(first[2]["url"])
        self.assertEqual(first[2]["external_url"], "https://notes.example/intro")
        # A unit without an attachment file is omitted.
        self.assertEqual([a["unit_id"] for a in lectures[1]["attachments"]], [7002])
        self.assertEqual(lectures[1]["attachments"][0]["url"], SLIDES_2)
        self.assertTrue(lectures[2]["is_tutorial"])
        self.assertEqual(lectures[2]["attachments"], [])

    def test_lecture_without_units_key_is_empty_not_an_error(self):
        # Real Artemis omits lectureUnits for a lecture with no content yet.
        for lecture in ({"id": 1, "title": "Empty"}, {"id": 1, "title": "Empty", "lectureUnits": None}):
            with self.subTest(lecture=lecture):
                parsed = artemis.parse_lectures([lecture], 7)
                self.assertEqual(parsed[0]["attachments"], [])
        with self.assertRaises(app.MoodleError):
            artemis.parse_lectures([{"id": 1, "lectureUnits": "x"}], 7)

    def test_student_url_rejects_paths_outside_attachments(self):
        for link in ("../../api/core/admin", "attachments/../core/x.pdf", "files/other/x.pdf",
                     "http://artemis.tum.de/x.pdf", "", None, "attachments/lecture//x.pdf"):
            with self.subTest(link=link):
                self.assertIsNone(artemis.student_file_url(link))
        self.assertEqual(artemis.student_file_url("/api/core/files/attachments/attachment-video-units/1/a b.pdf"),
                         BASE + "/api/core/files/attachments/attachment-video-units/1/student/a%20b.pdf")
        self.assertEqual(artemis.student_file_url("attachments/attachment-video-units/1/student/a.pdf"),
                         BASE + "/api/core/files/attachments/attachment-video-units/1/student/a.pdf")

    def test_layout_sorts_by_kind_and_keeps_names_readable(self):
        units = iter(range(1, 100))

        def lecture(title, *names, tutorial=False):
            # Every upload at the same minute: duplicates must fall back to the unit id.
            return {"title": title, "is_tutorial": tutorial, "course_id": 1, "attachments": [
                {"unit_id": next(units), "name": n, "filename": n + ".pdf", "uploaded": "2026-10-20 20-37",
                 "url": "u"} for n in names]}
        layout = artemis.material_layout([
            lecture("lec01 2627", "lec01 2627"),
            lecture("Tutorial 1", "Exercise Sheet", "Sample Solution"),
            lecture("Past Exams", "Endterm WS2025/26", "Short Test 1 WS2024/25"),
            lecture("Organisation", "Organisation"),
            lecture("Lecture 3: Examples of pipelining", "Slides"),
            lecture("Week 4", "Notes", tutorial=True),
            lecture("Übungsblatt 5", "Blatt 5"),
            lecture("Slide Set 2 – Microprocessors / Chips", "lec02"),
            lecture("Slide Set 2 – Microprocessors / Chips", "lec02"),
        ])
        self.assertEqual([f"{f['kind']}/{f['filename']}" for f in layout], [
            "Slides/lec01 2627.pdf",
            "Exercises/Tutorial 1 - Exercise Sheet.pdf", "Exercises/Tutorial 1 - Sample Solution.pdf",
            "Exams/Endterm WS2025-26.pdf", "Exams/Short Test 1 WS2024-25.pdf",
            "Other/Organisation.pdf",
            "Slides/Lecture 3 - Examples of pipelining - Slides.pdf",
            "Exercises/Week 4 - Notes.pdf",
            "Exercises/Übungsblatt 5.pdf",  # The shorter name is part of the title.
            # A re-upload with the same name: one file, the newest version (same minute -> higher unit id).
            "Slides/Slide Set 2 – Microprocessors - Chips - lec02.pdf",
        ])
        self.assertEqual(layout[-1]["unit_id"], 11)

    def test_newest_upload_wins_and_keeps_a_stable_sync_id(self):
        def deck(unit, uploaded, url):
            return {"title": "Deck", "course_id": 1, "attachments": [
                {"unit_id": unit, "name": "x", "filename": "x.pdf", "uploaded": uploaded, "url": url}]}
        # The older upload has the higher unit id here; the upload time decides.
        layout = artemis.material_layout([deck(9, "2026-10-20 20-20", "old"), deck(3, "2026-10-20 20-37", "new")])
        self.assertEqual([(f["filename"], f["url"]) for f in layout], [("Deck - x.pdf", "new")])
        again = artemis.material_layout([deck(5, "2026-10-27 09-00", "newer")])
        self.assertEqual(again[0]["sync_id"], layout[0]["sync_id"])
        other = artemis.material_layout([{"title": "Deck", "course_id": 1, "attachments": [
            {"unit_id": 5, "name": "y", "filename": "y.pdf", "uploaded": None, "url": "u"}]}])
        self.assertNotEqual(other[0]["sync_id"], layout[0]["sync_id"])
        self.assertGreater(layout[0]["sync_id"], 0)
        self.assertEqual(artemis.readable_name("A: b / c|d WS2025/26"), "A - b - c-d WS2025-26")

    def test_upload_time_comes_from_the_stored_link(self):
        lectures = artemis.parse_lectures(fixture("lectures.json"), 901)
        self.assertEqual(lectures[0]["attachments"][0]["uploaded"], "2026-10-13 10-00")
        self.assertIsNone(lectures[0]["attachments"][1]["uploaded"])

    def test_exercises_status_scores_and_order(self):
        now = artemis.datetime(2026, 10, 25, 12, tzinfo=artemis.timezone.utc)
        exercises = artemis.parse_exercises(fixture("exercises.json"), 901, now=now)
        self.assertEqual([e["id"] for e in exercises], [5104, 5101, 5102, 5103, 5105])
        by_id = {e["id"]: e for e in exercises}
        homework = by_id[5101]
        self.assertEqual((homework["status"], homework["counts"], homework["participation"]),
                         ("open", "graded", "result"))
        # Best rated graded result; the practice run (testRun) is reported separately.
        self.assertEqual((homework["score"], homework["practice_score"], homework["points"]), (80.0, 100.0, 8.0))
        self.assertEqual(homework["categories"], ["Homework", "plain-category"])
        self.assertEqual(homework["url"], BASE + "/courses/901/exercises/5101")
        quiz = by_id[5102]
        self.assertEqual((quiz["status"], quiz["counts"], quiz["participation"], quiz["score"]),
                         ("closed", "not_graded", "result", None))
        self.assertEqual((by_id[5103]["status"], by_id[5103]["counts"], by_id[5103]["participation"]),
                         ("not_released", "bonus", "not_started"))
        self.assertEqual((by_id[5104]["status"], by_id[5104]["participation"], by_id[5104]["points"]),
                         ("closed", "started", None))
        self.assertEqual((by_id[5105]["status"], by_id[5105]["due_date"]), ("open", None))

    def test_repo_url_accepts_only_plain_artemis_git_urls(self):
        good = BASE + "/git/IMALG26H01/imalg26h01-practice-ab12cde.git"
        self.assertEqual(artemis.repo_url(good), good)
        for bad in (None, 5, "", "http://artemis.tum.de/git/K/k.git", "https://evil.example/git/K/k.git",
                    BASE + "/git/K/k", BASE + "/git/K/sub/k.git", BASE + "/git/K/k.git?x=1",
                    "https://user@artemis.tum.de/git/K/k.git", BASE + "/git/../k.git", BASE + "/api/K/k.git"):
            with self.subTest(bad=bad):
                self.assertIsNone(artemis.repo_url(bad))

    def test_pick_repositories_separates_practice_from_graded(self):
        practice = BASE + "/git/IMALG26H01/imalg26h01-practice-ab12cde.git"
        graded = BASE + "/git/IMALG26H01/imalg26h01-ab12cde.git"
        self.assertEqual(artemis.pick_repositories([]), {"practice": None, "graded": None})
        self.assertEqual(artemis.pick_repositories([
            {"id": 2, "testRun": True, "initializationState": "INITIALIZED", "repositoryUri": practice},
            {"id": 1, "testRun": False, "initializationState": "FINISHED", "repositoryUri": graded},
        ]), {"practice": practice, "graded": graded})
        # Only a graded participation (e.g. a deadline still ahead): no practice repository.
        self.assertEqual(artemis.pick_repositories([{"id": 1, "testRun": False, "repositoryUri": graded}]),
                         {"practice": None, "graded": graded})
        # Several practice runs: the live one, then the latest attempt.
        old = BASE + "/git/IMALG26H01/imalg26h01-practice-old.git"
        self.assertEqual(artemis.pick_repositories([
            {"id": 5, "testRun": True, "attempt": 1, "initializationState": "INITIALIZED", "repositoryUri": old},
            {"id": 6, "testRun": True, "attempt": 2, "initializationState": "INITIALIZED", "repositoryUri": practice},
            {"id": 7, "testRun": True, "attempt": 3, "initializationState": "INACTIVE", "repositoryUri": old},
        ])["practice"], practice)
        # A URL outside artemis.tum.de/git never reaches the page.
        self.assertEqual(artemis.pick_repositories([
            {"id": 3, "testRun": True, "repositoryUri": "https://evil.example/x.git"}]),
            {"practice": None, "graded": None})

    def test_exercise_details_must_be_the_requested_exercise(self):
        for data in ([], {"exercise": None}, {"exercise": {"id": 6}},
                     {"exercise": {"id": 5, "studentParticipations": "x"}},
                     {"exercise": {"id": 5, "studentParticipations": ["x"]}}):
            with self.subTest(data=data), self.assertRaises(artemis.MoodleError):
                artemis.parse_exercise_repositories(data, 5)
        self.assertEqual(artemis.parse_exercise_repositories({"exercise": {"id": 5}}, 5),
                         {"practice": None, "graded": None})

    def test_exercises_absent_or_unknown_shape(self):
        # A course without exercises sends no "exercises" key at all (seen live).
        self.assertEqual(artemis.parse_exercises({"totalScores": {}}, 901), [])
        for data in ([], {"exercises": "unexpected"}):
            with self.assertRaises(app.MoodleError):
                artemis.parse_exercises(data, 901)

    def test_url_checks(self):
        for url in ("http://artemis.tum.de/api/x", "https://artemis.tum.de.example/", "https://user:pw@artemis.tum.de/",
                    "https://artemis.tum.de:8443/", "//artemis.tum.de/x", "https://www.moodle.tum.de/x", ""):
            with self.subTest(url=url), self.assertRaises(app.MoodleError):
                artemis.artemis_url(url)
        self.assertEqual(artemis.artemis_url("/api/x"), BASE + "/api/x")
        for url in (BASE + "/api/core/public/authenticate", BASE + "/api/course/courses/for-dashboard",
                    BASE + "/api/core/files/attachments/%2E%2E/x", BASE + "/api/core/files/markdown/x.png",
                    BASE + "/api/core/files/courses/901/conversations/301/%2E%2E",
                    BASE + "/api/core/files/courses/901/conversations/301/sub/x.pdf",
                    BASE + "/api/core/files/courses/x/conversations/301/a.pdf",
                    BASE + "/api/core/files/courses/901/a.pdf"):
            with self.subTest(url=url), self.assertRaises(app.MoodleError):
                artemis.file_url(url)
        channel_file = BASE + "/api/core/files/courses/901/conversations/301/2026-11-13T14-19-07-037_Notes.pdf"
        self.assertEqual(artemis.file_url(channel_file), channel_file)


class ClientTests(ArtemisTestCase):
    def test_client_is_lazy_and_logs_in_once(self):
        self.assertEqual(self.logins, 0)
        self.serve_course()
        self.assertEqual(len(self.call_tool("artemis_courses")), 2)
        self.call_tool("artemis_lectures", course="IMALG")
        self.call_tool("artemis_lectures", course="algorithms")
        self.assertEqual(self.logins, 1)
        # The course list is cached for lookups.
        self.assertEqual([c for c in self.calls if "for-dashboard" in c[1]].__len__(), 1)

    def test_exercises_tool_reads_the_course_overview(self):
        self.serve_course()
        self.routes[BASE + "/api/course/courses/901/exercises-for-overview"] = json_response(
            "", fixture("exercises.json"))
        exercises = self.call_tool("artemis_exercises", course="IMALG")
        self.assertEqual(len(exercises), 5)
        self.assertEqual({e["course_id"] for e in exercises}, {901})
        self.assertEqual(self.logins, 1)
        self.assertIsInstance(self.owner.read_only_tools()["artemis_exercises"](course="901"), list)

    def test_exercise_repos_reads_details_only_when_participations_change(self):
        self.serve_course()
        overview = fixture("exercises.json")
        self.routes[BASE + "/api/course/courses/901/exercises-for-overview"] = lambda url: json_response(url, overview)
        practice = BASE + "/git/IMALG26H01/imalg26h01-practice-ab12cde.git"
        graded = BASE + "/git/IMALG26H01/imalg26h01-ab12cde.git"
        details_5101 = {"exercise": {"id": 5101, "studentParticipations": [
            {"id": 8001, "testRun": False, "repositoryUri": graded},
            {"id": 8002, "testRun": True, "repositoryUri": practice}]}}
        self.routes[BASE + "/api/exercise/exercises/5101/details"] = json_response("", details_5101)
        # 5104's details are broken: only that exercise reports an error.
        self.routes[BASE + "/api/exercise/exercises/5104/details"] = json_response("", {"exercise": {"id": 1}})

        result = {e["id"]: e for e in self.call_tool("artemis_exercise_repos", course="901")}
        self.assertEqual(result[5101]["repositories"], {"practice": practice, "graded": graded})
        self.assertIn("repositories_error", result[5104])
        self.assertEqual(result[5105]["repositories"], {"practice": None, "graded": None})  # no participation
        self.assertNotIn("repositories_error", result[5102])  # quizzes have no repository
        details = lambda: sorted(u for m, u in self.calls if "/details" in u)
        self.assertEqual(details(), [BASE + "/api/exercise/exercises/5101/details",
                                     BASE + "/api/exercise/exercises/5104/details"])

        # Unchanged participations: 5101 comes from the cache, the failed 5104 is tried again.
        self.calls.clear()
        self.call_tool("artemis_exercise_repos", course="901")
        self.assertEqual(details(), [BASE + "/api/exercise/exercises/5104/details"])

        # A new participation (practice started on the web) is read again.
        overview["exercises"][0]["studentParticipations"].append({"id": 8009, "testRun": True})
        self.calls.clear()
        self.call_tool("artemis_exercise_repos", course="901")
        self.assertIn(BASE + "/api/exercise/exercises/5101/details", details())
        self.assertFalse(any(m != "GET" for m, _ in self.calls))  # never starts anything

    def test_rejected_password_is_not_retried_and_not_shown(self):
        self.login_status = 401
        result = self.call_tool("artemis_courses")
        self.assertEqual(set(result), {"error"})
        self.assertIn("Artemis", result["error"])
        self.assertNotIn(PASSWORD, result["error"])
        self.assertEqual(self.logins, 1)
        self.assertEqual(self.calls, [])

    def test_expired_cookie_relogs_once_then_reports(self):
        self.client.session.cookies.set("jwt", "expired-synthetic-cookie", domain=artemis.ARTEMIS_HOST)
        answers = iter([response("", status=401), json_response("", fixture("dashboard.json"))])
        self.routes[BASE + "/api/course/courses/for-dashboard"] = lambda url: next(answers)
        self.assertEqual(len(self.call_tool("artemis_courses")), 2)
        self.assertEqual(self.logins, 1)
        self.routes[BASE + "/api/course/courses/for-dashboard"] = response("", status=401)
        self.client.session.cookies.set("jwt", "expired-synthetic-cookie", domain=artemis.ARTEMIS_HOST)
        result = self.call_tool("artemis_courses")
        self.assertIn("error", result)
        self.assertEqual(self.logins, 2)

    def test_redirect_and_ambiguous_course_are_errors(self):
        self.routes[BASE + "/api/course/courses/for-dashboard"] = response(
            "", status=302, headers={"Location": "https://outside.example/"})
        self.assertIn("error", self.call_tool("artemis_courses"))
        self.serve_course()
        self.assertIn("several", self.call_tool("artemis_lectures", course="i")["error"])
        self.assertIn("No Artemis course", self.call_tool("artemis_lectures", course="nothing")["error"])

    def test_session_blocks_other_hosts_before_sending(self):
        client = artemis.TumArtemis(lambda: ("ab12cde", PASSWORD, None))
        self.addCleanup(client.session.close)
        request = app.requests.Request("GET", "https://outside.example/").prepare()
        with self.assertRaises(app.MoodleError):
            client.session.send(request)

    def test_errors_redact_jwt_cookie(self):
        self.serve_course()
        self.call_tool("artemis_courses")
        with patch.object(self.client, "lectures", side_effect=RuntimeError(f"bad {JWT}; jwt=other-synthetic-value")):
            message = self.call_tool("artemis_lectures", course="IMALG")["error"]
        self.assertNotIn(JWT, message)
        self.assertNotIn("other-synthetic-value", message)

    def test_download_tool_rejects_non_attachment_urls_without_login(self):
        for url in (BASE + "/api/course/courses/for-dashboard", "https://www.moodle.tum.de/pluginfile.php/1/x.pdf"):
            with self.subTest(url=url):
                self.assertIn("error", self.call_tool("artemis_download", url=url, dest_dir="unused"))
        self.assertEqual(self.logins, 0)
        self.assertEqual(self.calls, [])


class FileTests(ArtemisTestCase):
    def file_route(self, body=b"%PDF synthetic", modified="Sat, 17 Oct 2026 15:47:58 GMT"):
        return response(SLIDES_2, body=body, headers={
            "Content-Type": "application/pdf", "Last-Modified": modified,
            "Content-Disposition": 'inline; filename="lec02_2627.pdf"'})

    def test_download_uses_server_name_and_never_overwrites(self):
        root = self.temporary_directory()
        (root / "lec02_2627.pdf").write_bytes(b"Original")
        self.routes[SLIDES_2] = self.file_route()
        result = self.call_tool("artemis_download", url=SLIDES_2, dest_dir=str(root))
        self.assertEqual([Path(p).name for p in result], ["lec02_2627 (1).pdf"])
        self.assertEqual((root / "lec02_2627.pdf").read_bytes(), b"Original")
        self.assertEqual((root / "lec02_2627 (1).pdf").read_bytes(), b"%PDF synthetic")
        self.assertFalse(list(root.glob("*.part")))

    def test_download_accepts_channel_file_and_falls_back_to_url_name(self):
        root = self.temporary_directory()
        url = BASE + "/api/core/files/courses/901/conversations/301/2026-11-07T10-00-00-000_Sheet_1.pdf"
        self.routes[url] = response(url, body=b"%PDF channel", headers={"Content-Type": "application/pdf"})
        result = self.call_tool("artemis_download", url=url, dest_dir=str(root))
        self.assertEqual([Path(p).name for p in result], ["2026-11-07T10-00-00-000_Sheet_1.pdf"])
        self.assertEqual(Path(result[0]).read_bytes(), b"%PDF channel")

    def test_download_refuses_session_value_in_content(self):
        root = self.temporary_directory()
        self.routes[SLIDES_2] = lambda url: self.file_route(body=b"leak " + JWT.encode())
        self.assertIn("error", self.call_tool("artemis_download", url=SLIDES_2, dest_dir=str(root)))
        self.assertFalse((root / "lec02_2627.pdf").exists())

    def test_sync_preview_then_download_unchanged_and_update(self):
        root = self.temporary_directory() / "algorithms"
        self.serve_course()
        for lecture in fixture("lectures.json"):
            for unit in lecture["lectureUnits"]:
                url = artemis.student_file_url((unit.get("attachment") or {}).get("link"))
                if url:
                    self.routes[url] = response(url, body=b"file " + url.encode(), headers={
                        "Content-Type": "application/pdf", "Last-Modified": "Sat, 17 Oct 2026 15:47:58 GMT"})
        preview = self.call_tool("artemis_sync", course="IMALG", dest_dir=str(root))
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["new"]["count"], 3)
        self.assertEqual(sorted(preview["new"]["files"]), [
            "Exercises/Lecture 1 - Introduction - Exercise sheet 1.zip",
            "Slides/Lecture 1 - Introduction - Slides 1.pdf",
            "Slides/Lecture 2 - Sorting - Slides 2.pdf"])
        self.assertFalse(root.exists())
        self.assertFalse(any(method == "GET" and "/files/" in url for method, url in self.calls))

        first = self.call_tool("artemis_sync", course="IMALG", dest_dir=str(root), dry_run=False)
        self.assertEqual(first["new"]["count"], 3, first)
        self.assertTrue((root / ".artemis-sync-901.json").exists())
        self.assertFalse((root / ".moodle-sync.json").exists())
        self.assertNotIn(JWT, (root / ".artemis-sync-901.json").read_text(encoding="utf-8"))
        second = self.call_tool("artemis_sync", course="901", dest_dir=str(root), dry_run=False)
        self.assertEqual(second["unchanged"]["count"], 3)

        # A re-upload gets a new stored link: the old file goes to the Recycle Bin.
        trashed = []
        self.enterContext(patch.object(artemis.moodle_sync, "_trash", lambda path: (trashed.append(path.name), path.unlink())))
        lectures = fixture("lectures.json")
        unit = lectures[0]["lectureUnits"][0]
        unit["attachment"]["link"] = unit["attachment"]["link"].replace("2026-10-20", "2026-10-27")
        new_url = artemis.student_file_url(unit["attachment"]["link"])
        self.routes[new_url] = response(new_url, body=b"version two", headers={"Content-Type": "application/pdf"})
        self.serve_course(lectures)
        third = self.call_tool("artemis_sync", course="IMALG", dest_dir=str(root), dry_run=False)
        self.assertEqual(third["updated"]["files"], ["Slides/Lecture 2 - Sorting - Slides 2.pdf"])
        self.assertEqual((root / "Slides/Lecture 2 - Sorting - Slides 2.pdf").read_bytes(), b"version two")
        self.assertFalse((root / "Slides/Lecture 2 - Sorting - Slides 2_old.pdf").exists())
        self.assertEqual(trashed, ["Lecture 2 - Sorting - Slides 2_old.pdf"])

        # The instructor re-uploads into a new lecture with the same title: still the same file.
        copy = json.loads(json.dumps(lectures[0]))
        copy["id"], copy["lectureUnits"] = 5004, [copy["lectureUnits"][0]]
        copy["lectureUnits"][0]["id"] = 7010
        copy["lectureUnits"][0]["attachment"]["link"] = (
            "attachments/attachment-video-units/7010/AttachmentUnit_2026-10-28T09-00-00-000_lec02.pdf")
        newest_url = artemis.student_file_url(copy["lectureUnits"][0]["attachment"]["link"])
        self.routes[newest_url] = response(newest_url, body=b"version three", headers={"Content-Type": "application/pdf"})
        self.serve_course(lectures + [copy])
        fourth = self.call_tool("artemis_sync", course="IMALG", dest_dir=str(root), dry_run=False)
        self.assertEqual(fourth["updated"]["files"], ["Slides/Lecture 2 - Sorting - Slides 2.pdf"])
        self.assertEqual((fourth["new"]["count"], fourth["removed_online"]["count"]), (0, 0))
        self.assertEqual((root / "Slides/Lecture 2 - Sorting - Slides 2.pdf").read_bytes(), b"version three")
        self.assertEqual(self.logins, 1)

    def test_moodle_sync_still_rejects_artemis_urls(self):
        sync = app.moodle_sync
        with self.assertRaises(sync.SyncError):
            sync._safe_url(SLIDES_2, set())
        self.assertEqual(sync._safe_url(SLIDES_2, set(), self.client.sync_hosts), SLIDES_2)


class ForumTests(ArtemisTestCase):
    def serve_forum(self, data=None, headers=None):
        self.serve_course()
        self.routes[BASE + "/api/communication/courses/901/channels/overview"] = json_response("", fixture("channels.json"))
        result = json_response("", fixture("posts.json") if data is None else data)
        result.headers.update(headers or {})
        self.routes[BASE + "/api/communication/courses/901/messages"] = result

    def test_channels_and_replies_exclude_account_details_and_keep_context(self):
        self.serve_forum()
        channels = self.call_tool("artemis_channels", course="IMALG")
        self.assertEqual([c["id"] for c in channels], [301, 302, 303])
        self.assertTrue(channels[0]["can_read"])
        self.assertFalse(channels[-1]["can_read"])
        self.assertNotIn("not-exported", json.dumps(channels))
        result = self.call_tool("artemis_posts", course="901", channel="announcements")
        post = result["posts"][0]
        self.assertEqual(post["content"], fixture("posts.json")[0]["content"])
        self.assertEqual(post["author_role"], "INSTRUCTOR")
        self.assertTrue(post["pinned"])
        self.assertEqual([a["id"] for a in post["answers"]], [9001, 9002])
        self.assertTrue(post["answers"][1]["resolves_post"])
        self.assertTrue(result["posts"][1]["has_forwarded_messages"])
        # Only this course's channel files, each once; outside links stay in the text only.
        self.assertEqual(post["attachments"], [])
        self.assertEqual(result["posts"][1]["attachments"], [
            {"name": "Sheet 1.pdf", "image": False,
             "url": BASE + "/api/core/files/courses/901/conversations/301/2026-11-07T10-00-00-000_Sheet_1.pdf"},
            {"name": "Room map.png", "image": True,
             "url": BASE + "/api/core/files/courses/901/conversations/301/2026-11-07T10-00-01-000_Room_map.png"}])
        self.assertIn("conversationId=301&postId=8002", post["url"])
        self.assertNotIn("private-login", json.dumps(result))
        self.assertNotIn("private@example", json.dumps(result))
        self.assertEqual(self.logins, 1)

    def test_empty_search_is_sent_even_unfiltered_to_preserve_unread_state(self):
        self.serve_forum(headers={"X-Total-Count": "5"})
        result = self.owner.read_only_tools()["artemis_posts"](course="901", channel="301", page_size=2)
        kwargs = self.client.session.get.call_args.kwargs
        params = kwargs["params"]
        self.assertEqual(params["searchText"], "")
        self.assertEqual(params["conversationIds"], "301")
        self.assertEqual(params["sortingOrder"], "DESCENDING")
        prepared = app.requests.Request("GET", BASE + "/api/communication/courses/901/messages", params=params).prepare()
        self.assertIn("searchText=", prepared.url)
        self.assertEqual((result["total"], result["has_more"], result["next_page"]), (5, True, 1))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(self.logins, 1)  # Only POST was authentication, enforced by fake_post.

    def test_paging_search_and_omitted_total_do_not_claim_full_history(self):
        self.serve_forum()
        result = self.call_tool("artemis_posts", course="901", channel="#announcements", page=2, page_size=2, search="room")
        self.assertEqual(self.client.session.get.call_args.kwargs["params"]["page"], 2)
        self.assertEqual(self.client.session.get.call_args.kwargs["params"]["searchText"], "room")
        self.assertIsNone(result["total"])
        self.assertEqual(result["next_page"], 3)
        self.serve_forum(data=[], headers={"X-Total-Count": "0"})
        result = self.call_tool("artemis_posts", course="901", channel="301")
        self.assertEqual(result["posts"], [])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_page"])

    def test_bad_parameters_are_rejected_before_login(self):
        for kwargs in ({"page": -1}, {"page": True}, {"page": 10001}, {"page_size": 0},
                       {"page_size": 101}, {"page_size": True}, {"search": "x" * 501}, {"channel": ""}):
            result = self.call_tool("artemis_posts", **dict({"course": "901", "channel": "301"}, **kwargs))
            self.assertIn("error", result)
        self.assertEqual(self.logins, 0)

    def test_channel_must_belong_to_course_and_never_joins(self):
        self.serve_forum()
        for key in ("404", "../301", "https://other.example", "303"):
            self.assertIn("error", self.call_tool("artemis_posts", course="901", channel=key))
        self.assertFalse(any(url.endswith("/messages") for _, url in self.calls))
        self.assertEqual(self.logins, 1)

    def test_malformed_or_cross_channel_response_is_not_empty_success(self):
        bad = fixture("posts.json")
        bad[0]["conversation"]["id"] = 302
        for data in ({"posts": []}, [None], bad, [{"id": True}], [{"id": 1, "conversation": {"id": 301}, "answers": {}}]):
            self.serve_forum(data=data)
            self.assertIn("error", self.call_tool("artemis_posts", course="901", channel="301"))
        with self.assertRaises(app.MoodleError):
            artemis.parse_channels([{"id": 1, "name": "Private", "type": "oneToOneChat"}], 901)
        with self.assertRaises(app.MoodleError):
            artemis.parse_channels(fixture("channels.json") * 2, 901)

    def test_failed_or_invalid_pagination_is_explicit_and_errors_are_redacted(self):
        for total in ("not-a-count", "1"):
            self.serve_forum(headers={"X-Total-Count": total})
            self.assertIn("error", self.call_tool("artemis_posts", course="901", channel="301"))
        self.serve_forum()
        self.routes[BASE + "/api/communication/courses/901/messages"] = response("", status=403)
        self.assertIn("error", self.call_tool("artemis_posts", course="901", channel="301"))
        self.assertEqual(self.logins, 1)  # A permission refusal never repeats login.
        with patch.object(self.client, "posts", side_effect=RuntimeError("failed jwt=" + JWT)):
            result = self.call_tool("artemis_posts", course="901", channel="301")
        self.assertNotIn(JWT, json.dumps(result))

    def test_successful_forum_results_redact_both_clients_session_values(self):
        self.serve_forum()
        self.call_tool("artemis_channels", course="901")
        self.owner._moodle = app.make_client()
        self.addCleanup(self.owner._moodle.session.close)
        self.owner._moodle._sesskey = "fake-moodle-sesskey"
        self.owner._moodle.session.cookies.set("MoodleSession", "fake-moodle-cookie-value", domain=app.MOODLE_HOST)
        data = fixture("posts.json")
        data[0]["content"] = "Untrusted values " + JWT + " fake-moodle-sesskey fake-moodle-cookie-value"
        self.serve_forum(data=data)
        result = self.call_tool("artemis_posts", course="901", channel="301")
        self.assertNotIn(JWT, json.dumps(result))
        self.assertNotIn("fake-moodle-sesskey", json.dumps(result))
        self.assertNotIn("fake-moodle-cookie-value", json.dumps(result))
        self.assertIn("***", result["posts"][0]["content"])


class ToolDescriptionTests(ArtemisTestCase):
    def test_descriptions_mention_defaults_and_untrusted_content(self):
        tools = {t.name: t for t in self.server._tool_manager.list_tools()}
        self.assertTrue(tools["artemis_sync"].parameters["properties"]["dry_run"]["default"])
        self.assertIn("untrusted", tools["artemis_lectures"].description)
        self.assertIn("untrusted", tools["artemis_exercises"].description)
        self.assertIn("never submits", tools["artemis_exercises"].description)
        self.assertIn(".artemis-sync-<id>.json", tools["artemis_sync"].description)


if __name__ == "__main__":
    unittest.main()
