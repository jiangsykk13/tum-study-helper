"""Offline regression tests. Run with the project's Python and -I.

Only unittest is used as the test framework. All HTTP and credential access is
blocked; response bodies, cookie values and sesskeys below are synthetic.
"""

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().parents[1] / "tum_moodle_auto.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC = importlib.util.spec_from_file_location("tum_moodle_auto", SCRIPT)
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)


IMAGINARY = app.Course(7, "IMG", "Imaginary", app.MOODLE_URL + "/course/view.php?id=7")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def response(url, status=200, body=b"", headers=None, request=None):
    result = app.requests.Response()
    result.url = url
    result.status_code = status
    result.headers.update(headers or {})
    result._content = body
    result._content_consumed = True
    result.encoding = "utf-8"
    result.request = request
    return result


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.no_http = self.enterContext(patch(
            "requests.adapters.HTTPAdapter.send",
            side_effect=AssertionError("HTTP is forbidden in offline tests")))
        self.no_socket = self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("Network is forbidden")))
        self.no_socket_ex = self.enterContext(patch(
            "socket.socket.connect_ex", side_effect=AssertionError("Network is forbidden")))
        self.no_keyring = self.enterContext(patch.object(
            app.keyring, "get_password", side_effect=AssertionError("No keyring reads")))
        self.no_keyring_write = self.enterContext(patch.object(
            app.keyring, "set_password", side_effect=AssertionError("No keyring writes")))
        self.client = app.make_client()
        self.owner = app.TumMcpServer()
        self.owner._moodle = self.client
        self.server = self.owner.build_server()
        self.addCleanup(self.client.session.close)

    def tearDown(self):
        for blocked in (self.no_http, self.no_socket, self.no_socket_ex,
                        self.no_keyring, self.no_keyring_write):
            blocked.assert_not_called()

    def call_tool(self, name, **arguments):
        # Calling the actual registered wrapper avoids Windows asyncio's
        # internal loopback socketpair, so even local socket access stays blocked.
        return self.server._tool_manager.get_tool(name).fn(**arguments)

    def assert_error(self, result):
        self.assertEqual(set(result), {"error"})
        self.assertIsInstance(result["error"], str)
        self.assertTrue(result["error"])
        return result["error"]


class UrlTests(OfflineTestCase):
    def test_all_url_tools_reject_before_request_or_login(self):
        invalid = [
            "http://www.moodle.tum.de/mod/page/view.php?id=1",
            "https://outside.example/pluginfile.php/notes.pdf",
            "https://www.moodle.tum.de.outside.example/",
            "https://www.moodle.tum.de@outside.example/",
            "https://user:password@www.moodle.tum.de/",
            "https://moodle.tum.de/", "https://login.tum.de/",
            "https://tumidp.lrz.de/", "//outside.example/file",
            "//www.moodle.tum.de/file", "///outside.example/file",
            "mod/page/view.php?id=1", "www.moodle.tum.de/file",
            "https://www.moodle.tum.de:8443/",
            "https://www.moodle.tum.de:invalid/", "https://[broken/",
            "/\\outside.example/file", "\nhttps://www.moodle.tum.de/", "",
        ]
        with patch.object(self.client.session, "request") as request, \
                patch.object(self.client, "login") as login:
            for name in ("read_page", "list_files", "download"):
                for url in invalid:
                    with self.subTest(tool=name, url=url):
                        args = {"url": url}
                        if name == "download":
                            args["dest_dir"] = "unused-download-directory"
                        self.assert_error(self.call_tool(name, **args))
            request.assert_not_called()
            login.assert_not_called()

    def test_relative_and_full_urls_are_normalized(self):
        for url in ("/mod/page/view.php?id=1",
                    app.MOODLE_URL + "/mod/page/view.php?id=1"):
            with self.subTest(url=url), patch.object(
                    self.client.session, "request", return_value=response(
                        app.MOODLE_URL + "/mod/page/view.php?id=1",
                        body=b"<title>Test</title><div id='region-main'>Body</div>")) as request:
                result = self.call_tool("read_page", url=url)
                self.assertEqual(result["title"], "Test")
                self.assertEqual(result["text"], "Body")
                self.assertEqual(request.call_args.args[1],
                                 app.MOODLE_URL + "/mod/page/view.php?id=1")
        self.assertEqual(app.validated_url("/"), app.MOODLE_URL + "/")
        app.validated_url("https://WWW.MOODLE.TUM.DE:443/file")

    def test_relative_pluginfile_needs_no_request(self):
        with patch.object(self.client.session, "request") as request:
            files = self.call_tool("list_files", url="/pluginfile.php/1/notes.pdf")
            self.assertEqual(files, [{"name": "notes.pdf", "url":
                                     app.MOODLE_URL + "/pluginfile.php/1/notes.pdf"}])
            request.assert_not_called()

    def test_client_entry_points_reject_external_urls(self):
        url = "https://outside.example/pluginfile.php/notes.pdf"
        with patch.object(self.client.session, "request") as request:
            for action in (lambda: self.client.page(url),
                           lambda: self.client.files(url),
                           lambda: self.client.files(app.File("notes.pdf", url)),
                           lambda: self.client.download(url, "unused-download-directory"),
                           lambda: self.client.save(url, "unused-download-directory/notes.pdf")):
                with self.assertRaises(app.MoodleError):
                    action()
            request.assert_not_called()

    def test_external_redirect_is_blocked_before_second_send(self):
        def redirect(request, **kwargs):
            return response(request.url, status=302, headers={
                "Location": "https://outside.example/file?sesskey=fake-redirect-key"},
                request=request)

        with patch("requests.adapters.HTTPAdapter.send", side_effect=redirect) as send:
            self.assert_error(self.call_tool("read_page", url=app.MOODLE_URL + "/file"))
            self.assertEqual(send.call_count, 1)

    def test_login_hosts_only_allowed_during_sso(self):
        def success(request, **kwargs):
            return response(request.url, request=request)

        with patch("requests.adapters.HTTPAdapter.send", side_effect=success) as send:
            for host in ("login.tum.de", "tumidp.lrz.de"):
                url = "https://" + host + "/"
                with self.assertRaises(app.MoodleError):
                    self.client.session.get(url)
                with self.client.session.login_flow():
                    self.client.session.get(url)
                    with self.assertRaises(app.MoodleError):
                        self.client.session.get("https://outside.example/")
                with self.assertRaises(app.MoodleError):
                    self.client.session.get(url)
            self.assertEqual(send.call_count, 2)
            with self.assertRaises(RuntimeError):
                with self.client.session.login_flow():
                    raise RuntimeError("synthetic login error")
            self.assertEqual(self.client.session._allowed_hosts, {app.MOODLE_HOST})

    def test_resource_redirects_still_reject_unsafe_locations(self):
        url = app.MOODLE_URL + "/mod/resource/view.php?id=1"
        for location in ("https://outside.example/pluginfile.php/notes.pdf",
                         "http://www.moodle.tum.de/pluginfile.php/notes.pdf",
                         "https://outside.example/not-a-pluginfile"):
            for tool in ("list_files", "download"):
                with self.subTest(tool=tool, location=location), patch.object(
                        self.client.session, "request", return_value=response(
                            url, status=302, headers={"Location": location})) as request:
                    args = {"url": url}
                    if tool == "download":
                        args["dest_dir"] = "unused-download-directory"
                    self.assert_error(self.call_tool(tool, **args))
                    self.assertEqual(request.call_count, 1)

    def test_resource_redirect_accepts_safe_relative_location(self):
        url = app.MOODLE_URL + "/mod/resource/view.php?id=1"
        with patch.object(self.client, "get", return_value=response(
                url, status=302, headers={"Location": "/pluginfile.php/1/notes.pdf"})) as get:
            self.assertEqual(self.call_tool("list_files", url=url), [
                {"name": "notes.pdf", "url": app.MOODLE_URL + "/pluginfile.php/1/notes.pdf"}])
            get.assert_called_once_with(url, allow_redirects=False)

    def test_html_file_links_skip_unsafe_urls_and_keep_safe_files(self):
        url = app.MOODLE_URL + "/mod/folder/view.php?id=1"
        body = """<a href='/pluginfile.php/1/outside-main.pdf'>Not in main</a>
            <div id='region-main'>
              <a href='https://outside.example/pluginfile.php/unsafe.pdf'>Other host</a>
              <a href='http://www.moodle.tum.de/pluginfile.php/insecure.pdf'>HTTP</a>
              <a href='https://[broken/pluginfile.php/invalid.pdf'>Malformed</a>
              <a href='/pluginfile.php/1/notes.pdf'>Safe relative</a>
              <a href='https://www.moodle.tum.de/pluginfile.php/1/notes.pdf'>Duplicate</a>
              <object data='/pluginfile.php/1/model.pdf'></object>
              <iframe src='https://outside.example/pluginfile.php/frame.pdf'></iframe>
              <iframe src='/pluginfile.php/1/frame.pdf'></iframe>
              <embed src='/pluginfile.php/1/chart.pdf'>
              <a href='/mod/page/view.php?id=2'>Not a file</a>
            </div>"""
        expected = [{"name": name, "url": app.MOODLE_URL + "/pluginfile.php/1/" + name}
                    for name in ("notes.pdf", "model.pdf", "frame.pdf", "chart.pdf")]
        with patch.object(self.client, "get", return_value=response(url, body=body.encode())) as get:
            self.assertEqual(self.call_tool("list_files", url=url), expected)
            get.assert_called_once_with(url, allow_redirects=False)
            with patch.object(self.client, "save", return_value=Path("synthetic-saved-file")) as save:
                results = self.call_tool("download", url=url, dest_dir="unused-download-directory")
                self.assertEqual(len(results), len(expected))
                self.assertEqual([c.args[0].url for c in save.call_args_list],
                                 [f["url"] for f in expected])

    def test_html_with_only_unsafe_links_returns_no_files(self):
        url = app.MOODLE_URL + "/mod/page/view.php?id=1"
        body = (b"<div id='region-main'><a href='https://outside.example/"
                b"pluginfile.php/notes.pdf'>Notes</a></div>")
        with patch.object(self.client, "get", return_value=response(url, body=body)), \
                patch.object(self.client, "save") as save:
            self.assertEqual(self.call_tool("list_files", url=url), [])
            self.assertEqual(self.call_tool("download", url=url,
                                            dest_dir="unused-download-directory"), [])
            save.assert_not_called()

    def test_download_preserves_unique_destination_selection(self):
        # Keep the library's no-overwrite path selection without writing files.
        original = Path("offline-download") / "notes.pdf"
        with patch.object(Path, "exists", side_effect=[True, False]):
            self.assertEqual(app.Utils.unique_path(original), original.with_name("notes (1).pdf"))
        with patch.object(self.client, "files", return_value=[app.File(
                "notes.pdf", app.MOODLE_URL + "/pluginfile.php/1/notes.pdf")]), \
                patch.object(app.Utils, "unique_path", return_value=original.with_name("notes (1).pdf")), \
                patch.object(self.client, "save", return_value=original.with_name("notes (1).pdf")) as save:
            self.call_tool("download", url="/pluginfile.php/1/notes.pdf", dest_dir="offline-download")
            self.assertEqual(save.call_args.args[1], original.with_name("notes (1).pdf"))


class ErrorTests(OfflineTestCase):
    TOOL_CALLS = [
        ("list_courses", "courses", {}),
        ("list_activities", "activities", {"course": "Test"}),
        ("list_files", "files", {"url": app.MOODLE_URL + "/file"}),
        ("read_page", "page", {"url": app.MOODLE_URL + "/page"}),
        ("download", "download", {"url": app.MOODLE_URL + "/file", "dest_dir": "."}),
        ("upcoming_deadlines", "deadlines", {}),
        ("grades", "grades", {}),
        ("assignments", "assignments", {"course": "Test"}),
        ("sync_course", "sync_course", {"course": "Test", "dest_dir": "unused"}),
    ]

    def test_all_tools_wrap_common_and_unexpected_exceptions(self):
        errors = [app.requests.Timeout("synthetic timeout"),
                  app.requests.ConnectionError("synthetic connection failure"),
                  app.requests.HTTPError("synthetic HTTP failure"),
                  json.JSONDecodeError("synthetic invalid JSON", "{", 1),
                  app.requests.exceptions.JSONDecodeError("invalid JSON", "{", 1),
                  ImportError("synthetic missing dependency"),
                  OSError("synthetic write failure"), ValueError("synthetic parsing failure"),
                  RuntimeError("synthetic unexpected failure"), app.MoodleError("Moodle error")]
        for tool, method, args in self.TOOL_CALLS:
            for error in errors:
                with self.subTest(tool=tool, error=type(error).__name__), patch.object(
                        self.client, method, side_effect=error):
                    self.assert_error(self.call_tool(tool, **args))

    def test_actual_http_errors_are_wrapped_and_url_sesskey_is_redacted(self):
        for status in (400, 401, 403, 404, 500, 503):
            url = app.MOODLE_URL + "/lib/ajax/service.php?sesskey=fake-http-key&info=test"
            with self.subTest(status=status), patch.object(
                    self.client.session, "request", return_value=response(url, status=status)):
                message = self.assert_error(self.call_tool("read_page", url=url))
                self.assertNotIn("fake-http-key", message)
                self.assertIn("sesskey=***", message)
                self.assertIn(str(status), message)

    def test_ajax_json_failure_is_wrapped(self):
        self.client._sesskey = "fake-ajax-key"
        url = app.MOODLE_URL + "/lib/ajax/service.php"
        with patch.object(self.client.session, "request", return_value=response(url, body=b"not JSON")):
            self.assert_error(self.call_tool("list_courses"))

    def test_cookie_and_sesskey_are_redacted_for_all_tools(self):
        self.client._sesskey = "fake-private-key"
        self.client.session.cookies.set("MoodleSessionTest", "fake-private-cookie",
                                        domain=app.MOODLE_HOST)
        for tool, method, args in self.TOOL_CALLS:
            with self.subTest(tool=tool), patch.object(self.client, method, side_effect=RuntimeError(
                    "URL ?sesskey=fake-private-key&info=x; bare fake-private-cookie; "
                    "Cookie: arbitrary_cookie=fake-header-cookie")):
                message = self.assert_error(self.call_tool(tool, **args))
                for value in ("fake-private-key", "fake-private-cookie", "fake-header-cookie"):
                    self.assertNotIn(value, message)
                self.assertIn("***", message)

    def test_labelled_and_encoded_secrets_without_session_state(self):
        errors = [
            "URL ?sesskey=fake-key&info=method",
            "URL ?SESSKEY=fake-key&info=method",
            'JSON {"sesskey": "fake-key"}',
            "dict {'sesskey': 'fake-key'}",
            "MoodleSessionXYZ=fake-cookie; Path=/",
            "Cookie: OtherName=fake-cookie; MoodleSession=fake-cookie",
            "headers {'Cookie': 'OtherName=fake-cookie'}",
            "Set-Cookie: OtherName=fake-cookie; Secure",
            "URL ?sesskey%3Dfake-key%26info%3Dmethod",
            "URL ?sesskey%253Dfake-key%2526info%253Dmethod",
        ]
        for text in errors:
            with self.subTest(text=text):
                message = app.safe_error(RuntimeError(text))
                self.assertNotIn("fake-key", message)
                self.assertNotIn("fake-cookie", message)
                self.assertIn("***", message)
        self.assertNotIn("fake/key", app.safe_error(RuntimeError("bare fake%2Fkey"), {"fake/key"}))
        for value in ("fake%2Fcookie", "fake/cookie", "fake%252Fcookie"):
            self.assertNotIn("fake", app.safe_error(RuntimeError("bare " + value), {"fake%2Fcookie"}))

    def test_old_session_values_are_redacted_after_login_clears_them(self):
        self.client._sesskey = "fake-old-key"
        self.client.session.cookies.set("MoodleSessionTest", "fake-old-cookie")

        def failed_operation(*args):
            self.client._sesskey = None
            self.client.session.cookies.clear()
            raise app.NotLoggedIn("bare fake-old-key and fake-old-cookie")

        with patch.object(self.client, "courses", side_effect=failed_operation), \
                patch.object(self.client, "login") as login:
            message = self.assert_error(self.call_tool("list_courses"))
            self.assertNotIn("fake-old-key", message)
            self.assertNotIn("fake-old-cookie", message)
            login.assert_not_called()

    def test_failed_automatic_login_is_not_retried_by_tool_wrapper(self):
        login_page = response(app.MOODLE_URL + "/login/index.php")
        with patch.object(self.client.session, "request", return_value=login_page), \
                patch.object(self.client, "login", side_effect=app.NotLoggedIn("synthetic rejection")) as login:
            self.assert_error(self.call_tool("read_page", url="/page"))
            self.assertEqual(login.call_count, 1)

    def test_successful_lazy_login_retries_request_once(self):
        replies = [response(app.MOODLE_URL + "/login/index.php"),
                   response(app.MOODLE_URL + "/page", body=b"<title>Logged in</title>")]
        with patch.object(self.client.session, "request", side_effect=replies) as request, \
                patch.object(self.client, "login") as login:
            self.assertEqual(self.call_tool("read_page", url="/page")["title"], "Logged in")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(login.call_count, 1)

    def test_result_conversion_and_error_formatting_failures_are_wrapped(self):
        with patch.object(self.client, "courses", return_value=[]), \
                patch.object(app.Utils, "to_dict", side_effect=ValueError("synthetic conversion failure")):
            self.assert_error(self.call_tool("list_courses"))

        class BrokenError(Exception):
            def __str__(self):
                raise ValueError("synthetic formatting failure")

        with patch.object(self.client, "courses", side_effect=BrokenError()):
            self.assert_error(self.call_tool("list_courses"))


class CourseCacheTests(OfflineTestCase):
    @staticmethod
    def course_data():
        return {"courses": [
            {"id": 1, "shortname": "ML", "fullname": "Machine Learning", "viewurl": app.MOODLE_URL + "/course/1"},
            {"id": 2, "shortname": "CV", "fullname": "Computer Vision", "viewurl": app.MOODLE_URL + "/course/2"},
        ]}

    def test_lookup_cache_is_reused_and_expires_at_ten_minutes(self):
        clock = Mock(return_value=1000.0)
        with patch.object(app.time, "monotonic", clock), patch.object(
                self.client, "call", return_value=self.course_data()) as call:
            self.assertEqual(self.client.course("ml").id, 1)
            self.assertEqual(self.client.course("Vision").id, 2)
            clock.return_value = 1599.0
            self.assertEqual(self.client.course(1).id, 1)
            self.assertEqual(call.call_count, 1)
            clock.return_value = 1600.0
            self.assertEqual(self.client.course("CV").id, 2)
            self.assertEqual(call.call_count, 2)
            self.assertTrue(all(c.kwargs["classification"] == "all" for c in call.call_args_list))

    def test_explicit_listing_stays_fresh_and_refreshes_all_cache(self):
        with patch.object(self.client, "call", return_value=self.course_data()) as call:
            self.client.course("ML")
            self.client.courses("inprogress")
            self.client.courses("all")
            self.client.course("CV")
            self.assertEqual(call.call_count, 3)

    def test_course_objects_and_lookup_errors_preserve_existing_behavior(self):
        course = app.Course(1, "ML", "Machine Learning", app.MOODLE_URL + "/course/1")
        with patch.object(self.client, "call", return_value=self.course_data()) as call:
            self.assertIs(self.client.course(course), course)
            call.assert_not_called()
            with self.assertRaisesRegex(app.MoodleError, "No course matches"):
                self.client.course("Unknown")
            with self.assertRaisesRegex(app.MoodleError, "matches several courses"):
                self.client.course("")
            self.assertEqual(call.call_count, 1)

    def test_failed_fetch_does_not_poison_cache_and_empty_results_are_cached(self):
        with patch.object(self.client, "call", side_effect=[RuntimeError("synthetic fetch failure"),
                                                           {"courses": []}]) as call:
            with self.assertRaises(RuntimeError):
                self.client.course("ML")
            self.assertIsNone(self.client._courses_cache)
            for _ in range(2):
                with self.assertRaises(app.MoodleError):
                    self.client.course("ML")
            self.assertEqual(call.call_count, 2)

    def test_login_invalidates_cache_without_reading_credentials(self):
        with patch.object(self.client, "call", return_value=self.course_data()) as call, \
                patch.object(app.TumSso, "login") as login:
            self.client.course("ML")
            self.client._sesskey = "fake-cache-key"
            self.client.login()
            self.assertIsNone(self.client._sesskey)
            self.assertIsNone(self.client._courses_cache)
            self.client.course("ML")
            self.assertEqual(call.call_count, 2)
            self.assertEqual(login.call_count, 1)


class DeadlineTests(OfflineTestCase):
    @staticmethod
    def event(event_id):
        return {"id": event_id, "name": f"Task {event_id} &amp; quiz",
                "course": {"fullname": "Course &amp; lab"}, "timesort": 2000,
                "url": app.MOODLE_URL + f"/mod/assign/view.php?id={event_id}", "modulename": "assign"}

    def test_over_fifty_events_are_merged_using_aftereventid(self):
        pages = [{"events": [self.event(i) for i in range(1, 51)]},
                 {"events": [self.event(i) for i in range(51, 101)]},
                 {"events": [self.event(i) for i in range(101, 126)]}, {"events": []}]
        with patch.object(app.time, "time", return_value=1000), patch.object(
                self.client, "call", side_effect=pages) as call:
            results = self.call_tool("upcoming_deadlines", days=7)
            self.assertEqual(len(results), 125)
            self.assertEqual(results[0]["name"], "Task 1 & quiz")
            self.assertEqual(results[-1]["name"], "Task 125 & quiz")
            self.assertEqual(results[0]["course"], "Course & lab")
            self.assertEqual([c.kwargs["aftereventid"] for c in call.call_args_list], [0, 50, 100, 125])
            for c in call.call_args_list:
                self.assertEqual(c.args, ("core_calendar_get_action_events_by_timesort",))
                self.assertEqual(c.kwargs["timesortfrom"], 1000)
                self.assertEqual(c.kwargs["timesortto"], 1000 + 7 * 86400)
                self.assertEqual(c.kwargs["limitnum"], 50)

    def test_short_pages_and_overlapping_events_do_not_truncate_or_duplicate(self):
        pages = [{"events": [self.event(8), self.event(3)]},
                 {"events": [self.event(3), self.event(7)]}, {"events": []}]
        with patch.object(self.client, "call", side_effect=pages) as call:
            results = self.client.deadlines()
            self.assertEqual([e.name for e in results], ["Task 8 & quiz", "Task 3 & quiz", "Task 7 & quiz"])
            self.assertEqual([c.kwargs["aftereventid"] for c in call.call_args_list], [0, 3, 7])

    def test_empty_first_page(self):
        with patch.object(self.client, "call", return_value={"events": []}) as call:
            self.assertEqual(self.client.deadlines(), [])
            self.assertEqual(call.call_count, 1)

    def test_repeated_cursor_is_reported_without_looping(self):
        page = {"events": [self.event(1)]}
        with patch.object(self.client, "call", return_value=page) as call:
            self.assert_error(self.call_tool("upcoming_deadlines"))
            self.assertEqual(call.call_count, 2)

    def test_second_page_failure_returns_an_error(self):
        with patch.object(self.client, "call", side_effect=[{"events": [self.event(1)]},
                                                           app.requests.Timeout("synthetic page timeout")]):
            self.assert_error(self.call_tool("upcoming_deadlines"))


class GradeTests(OfflineTestCase):
    def test_overview_skips_emptyrow_and_preserves_localized_grades(self):
        self.assertEqual(app.parse_grade_overview(fixture("grades_overview.html")), [
            {"course_name": "Imaginary Robotics & Clouds", "course_id": 91001, "grade": "1,7"},
            {"course_name": "Imaginary Moon Gardening", "course_id": 91002, "grade": None},
            {"course_name": "Imaginary Crystal Computing", "course_id": 91003, "grade": None},
            {"course_name": "Imaginary Star Logistics", "course_id": 91004, "grade": None},
        ])

    def test_course_rows_categories_totals_and_menu_removal(self):
        rows = app.parse_course_grades(fixture("grades_course.html"))
        self.assertEqual([r["name"] for r in rows], [
            "Imaginary warm-up", "Imaginary cloud model", "Imaginary unfinished quiz",
            "Imaginary exercises total", "Imaginary zero-score quiz", "Imaginary course total"])
        self.assertEqual([r["type"] for r in rows],
                         ["item", "item", "item", "category_total", "item", "course_total"])
        self.assertEqual([r["category"] for r in rows], [
            None, "Imaginary exercises & models", "Imaginary exercises & models",
            "Imaginary exercises & models", "Imaginary exams", None])
        self.assertEqual(rows[1], {
            "name": "Imaginary cloud model", "type": "item",
            "category": "Imaginary exercises & models",
            "url": app.MOODLE_URL + "/mod/assign/view.php?id=92001", "type_label": "Aufgabe",
            "weight": "40,0 %", "grade": "1,7", "range": "1–5", "percentage": None,
            "letter_grade": "B", "feedback": "Clear model. Try another example.",
            "contribution": "0,68",
        })
        for row in rows:
            self.assertIsNone(row["percentage"])
        for field in ("weight", "grade", "letter_grade", "feedback", "contribution"):
            self.assertIsNone(rows[2][field])
        self.assertEqual(rows[2]["range"], "0–100")
        self.assertEqual(rows[2]["type_label"], "Test")
        self.assertEqual(rows[4]["grade"], "0")
        self.assertEqual(rows[5]["grade"], "1,7")
        for row in (rows[0], rows[3], rows[5]):
            self.assertIsNone(row["url"])
            self.assertIsNone(row["type_label"])
        for field in ("weight", "grade", "range", "percentage", "letter_grade", "feedback", "contribution"):
            self.assertIsNone(rows[0][field])

    def test_percentage_and_reordered_columns_ignore_header_text(self):
        self.assertEqual(app.parse_course_grades(fixture("grades_course_percentage.html")), [{
            "name": "Imaginary discussion", "type": "item", "category": None,
            "url": app.MOODLE_URL + "/mod/forum/view.php?id=92011", "type_label": "Forum",
            "weight": "50,0 %", "grade": "17", "range": "0–20", "percentage": "85,0 %",
            "letter_grade": "A", "feedback": "Useful discussion.", "contribution": "42,5 %",
        }])

    def test_empty_tables_and_missing_tables_are_distinguished(self):
        for parser, empty_html in (
                (app.parse_grade_overview, '<table id="overview-grade"><tbody></tbody></table>'),
                (app.parse_course_grades, '<table class="user-grade"><tbody></tbody></table>')):
            with self.subTest(parser=parser.__name__):
                self.assertEqual(parser(empty_html), [])
                with self.assertRaises(app.MoodleError):
                    parser("<html><body>No grade report</body></html>")

    def test_overview_tool_fetches_html_without_course_lookup_or_ajax(self):
        html_text = fixture("grades_overview.html")
        with patch.object(self.client, "get", return_value=Mock(text=html_text)) as get, \
                patch.object(self.client, "course") as course, patch.object(self.client, "call") as ajax:
            self.assertEqual(self.call_tool("grades"), app.parse_grade_overview(html_text))
            get.assert_called_once_with("/grade/report/overview/index.php")
            course.assert_not_called()
            ajax.assert_not_called()

    def test_course_tool_uses_existing_id_shortname_and_partial_name_lookup(self):
        self.client._courses_cache = [app.Course(
            91001, "IRC", "Imaginary Robotics & Clouds", app.MOODLE_URL + "/course/view.php?id=91001")]
        self.client._courses_cached_at = app.time.monotonic()
        html_text = fixture("grades_course.html")
        for key in ("91001", "irc", "Robotics"):
            with self.subTest(course=key), patch.object(
                    self.client, "get", return_value=Mock(text=html_text)) as get, \
                    patch.object(self.client, "course", wraps=self.client.course) as course, \
                    patch.object(self.client, "call") as ajax:
                self.assertEqual(self.call_tool("grades", course=key), app.parse_course_grades(html_text))
                course.assert_called_once_with(key)
                get.assert_called_once_with("/grade/report/user/index.php?id=91001")
                ajax.assert_not_called()

    def test_grades_get_errors_are_guarded_and_redacted(self):
        self.client._sesskey = "fake-grades-key"
        with patch.object(self.client, "get", side_effect=app.requests.Timeout(
                "synthetic timeout with fake-grades-key")):
            message = self.assert_error(self.call_tool("grades"))
            self.assertNotIn("fake-grades-key", message)
            self.assertIn("***", message)

    def test_missing_report_is_guarded(self):
        with patch.object(self.client, "get", return_value=Mock(text="<body>Unrelated page</body>")):
            self.assert_error(self.call_tool("grades"))


class CompatibilityTests(OfflineTestCase):
    def test_server_lazy_client_uses_project_overrides(self):
        owner = app.TumMcpServer()
        self.assertIsNone(owner._moodle)
        try:
            self.assertIsInstance(owner.moodle, app.TumMoodle)
            self.assertIs(owner.moodle, owner._moodle)
        finally:
            if owner._moodle is not None:
                owner._moodle.session.close()

    def test_library_methods_are_not_monkeypatched(self):
        self.assertEqual(app.Moodle.login.__module__, "moodlekit.client")
        self.assertEqual(app.Moodle._load_cookies.__module__, "moodlekit.client")

    def test_tool_names_parameters_and_defaults_are_unchanged(self):
        tools = self.server._tool_manager.list_tools()
        expected = {
            "list_courses": {"which"}, "list_activities": {"course"},
            "list_files": {"url"}, "read_page": {"url"},
            "download": {"url", "dest_dir"}, "upcoming_deadlines": {"days"},
            "grades": {"course"},
            "assignments": {"course"}, "sync_course": {"course", "dest_dir", "dry_run"},
            "artemis_courses": set(), "artemis_lectures": {"course"}, "artemis_exercises": {"course"}, "artemis_exercise_repos": {"course"},
            "artemis_channels": {"course"}, "artemis_posts": {"course", "channel", "page", "page_size", "search"},
            "artemis_download": {"url", "dest_dir"},
            "artemis_sync": {"course", "dest_dir", "dry_run"},
        }
        self.assertEqual({t.name for t in tools}, set(expected))
        for tool in tools:
            self.assertEqual(set(tool.parameters["properties"]), expected[tool.name])
        schemas = {t.name: t.parameters for t in tools}
        self.assertEqual(schemas["list_courses"]["properties"]["which"]["default"], "inprogress")
        self.assertEqual(schemas["upcoming_deadlines"]["properties"]["days"]["default"], 30)
        self.assertEqual(schemas["grades"]["properties"]["course"]["default"], "")
        self.assertTrue(schemas["sync_course"]["properties"]["dry_run"]["default"])
        # wraps also preserves the callable signature used to generate MCP schemas.
        def example(url: str, dest_dir: str):
            return []
        self.assertEqual(inspect.signature(self.owner._guard_tool(example)), inspect.signature(example))

    def test_read_only_server_offers_view_tools_with_unchanged_schemas(self):
        read_only = {t.name: t.parameters for t in self.owner.build_server(read_only=True)._tool_manager.list_tools()}
        full = {t.name: t.parameters for t in self.server._tool_manager.list_tools()}
        self.assertEqual(set(read_only), set(app.READ_ONLY_TOOLS))
        self.assertFalse({"download", "sync_course", "artemis_download", "artemis_sync"} & set(read_only))
        for name, schema in read_only.items():
            self.assertEqual(schema, full[name])

    def test_activity_description_lists_types_and_explains_availability(self):
        description = self.server._tool_manager.get_tool("list_activities").description
        for word in ("files", "folders", "assignments", "forums", "available=false", "not yet open"):
            self.assertIn(word, description)


class FinalFeatureTests(OfflineTestCase):
    def temporary_directory(self):
        # Python's Windows 0700 temp ACL omits the restricted sandbox identity.
        # Inherit the private system-temp ACL during creation only; restore mkdir
        # before exercising production code. No application ACLs are changed.
        mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw:
                   mkdir(path, 0o777, **kw)):
            return Path(self.enterContext(tempfile.TemporaryDirectory(prefix="moodle-integration-")))

    @staticmethod
    def activity(cmid, kind="assign", available=True):
        return app.Activity(cmid, f"Imaginary {kind} {cmid}", kind,
                            app.MOODLE_URL + f"/mod/{kind}/view.php?id={cmid}",
                            "Imaginary section", 1, available, 7)

    def test_assignments_only_fetches_available_assignments_and_keeps_unknowns(self):
        activities = [self.activity(1), self.activity(2, available=False),
                      self.activity(3, "resource")]
        source = fixture("assign/submitted_graded.html")
        with patch.object(self.client, "activities", return_value=activities), \
                patch.object(self.client, "get", return_value=Mock(text=source)) as get:
            rows = self.call_tool("assignments", course="Imaginary")
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["submission"], "submitted")
        self.assertEqual(rows[0]["grading"], "graded")
        self.assertTrue(rows[0]["fetched"])
        self.assertFalse(rows[1]["fetched"])
        self.assertFalse(rows[1]["available"])
        self.assertEqual(rows[1]["submission"], "unknown")
        self.assertIsNone(rows[1]["grade"])
        get.assert_called_once_with(activities[0].url)

    def test_assignment_failure_is_redacted_and_does_not_hide_later_assignments(self):
        self.client._sesskey = "synthetic-assignment-key"
        self.client.session.cookies.set("MoodleSession", "synthetic-assignment-cookie")
        with patch.object(self.client, "activities", return_value=[self.activity(1), self.activity(2)]), \
                patch.object(self.client, "get", side_effect=[
                    RuntimeError("synthetic-assignment-key synthetic-assignment-cookie"),
                    Mock(text=fixture("assign/draft.html"))]):
            rows = self.call_tool("assignments", course="Imaginary")
        self.assertNotIn("synthetic-assignment", rows[0]["error"])
        self.assertIn("***", rows[0]["error"])
        self.assertEqual(rows[1]["submission"], "draft")

    def test_assignment_external_url_is_rejected_before_request(self):
        activity = self.activity(1)
        activity.url = "https://outside.example/assignment"
        with patch.object(self.client, "activities", return_value=[activity]), \
                patch.object(self.client, "get") as get:
            rows = self.call_tool("assignments", course="Imaginary")
        self.assertIn("error", rows[0])
        get.assert_not_called()

    def test_course_state_labels_are_merged_in_original_positions_with_one_ajax(self):
        selected = app.Course(7, "IC", "Imaginary course", app.MOODLE_URL + "/course/view.php?id=7")
        state = {"cm": [
            {"id": 3, "module": "label", "name": "Last", "sectionnumber": 1, "uservisible": False},
            {"id": 2, "module": "resource", "name": "Notes", "url": self.activity(2, "resource").url},
            {"id": 1, "module": "label", "name": "First &amp; note", "sectionnumber": 1},
        ], "section": [{"number": 1, "title": "Imaginary section", "cmlist": [1, 2, 3]}]}
        with patch.object(self.client, "course", return_value=selected), \
                patch.object(self.client, "call", return_value=json.dumps(state)) as call:
            rows = self.call_tool("list_activities", course="IC")
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["name"], "First & note")
        self.assertEqual(rows[0]["type"], "label")
        self.assertIsNone(rows[0]["url"])
        self.assertFalse(rows[2]["available"])
        self.assertEqual(set(rows[0]), set(vars(self.activity(1))))
        call.assert_called_once_with("core_courseformat_get_state", courseid=7)

    def test_html_fallback_keeps_labels_between_modules_and_ignores_links_in_labels(self):
        selected = app.Course(7, "IC", "Imaginary course", app.MOODLE_URL + "/course/view.php?id=7")
        source = """<li class='section'><h3 class='sectionname'>Section</h3>
          <li id='module-1' class='activity modtype_resource'><a href='/mod/resource/view.php?id=1'>Notes</a></li>
          <li id='module-2' class='activity label modtype_label'><div class='activity-altcontent'>
          Full label <a href='/mod/page/view.php?id=99'>reference</a></div></li>
          <li id='module-3' class='activity modtype_assign'><a href='/mod/assign/view.php?id=3'>Homework</a></li></li>"""
        with patch.object(self.client, "course", return_value=selected), \
                patch.object(self.client, "call", side_effect=app.MoodleError("unavailable")), \
                patch.object(self.client, "get", return_value=Mock(text=source)) as get:
            rows = self.call_tool("list_activities", course="IC")
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[1]["name"], "Full label reference")
        self.assertEqual(rows[1]["type"], "label")
        self.assertIsNone(rows[1]["url"])
        get.assert_called_once_with("/course/view.php?id=7")

    def test_read_page_renders_markdown_table_and_preserves_title_and_link_list(self):
        source = """<title>Imaginary page</title><main id='region-main'><h2>Schedule</h2>
          <table><tr><th>Week</th><th>Work</th></tr><tr><td>1</td><td>Read<br>Discuss</td></tr></table>
          <ul><li>Bring notes</li></ul><span class='visually-hidden'>Hidden</span>
          <a href='/mod/page/view.php?id=2'>Next</a><a href='/pluginfile.php/1/a.pdf'>PDF</a></main>"""
        with patch.object(self.client, "get", return_value=Mock(text=source)) as get:
            page = self.call_tool("read_page", url="/mod/page/view.php?id=1")
        self.assertEqual(page["title"], "Imaginary page")
        self.assertIn("## Schedule", page["text"])
        self.assertIn("| Week | Work |\n| --- | --- |\n| 1 | Read<br>Discuss |", page["text"])
        self.assertIn("- Bring notes", page["text"])
        self.assertNotIn("Hidden", page["text"])
        self.assertEqual(page["links"], [
            {"text": "Next", "url": app.MOODLE_URL + "/mod/page/view.php?id=2"},
            {"text": "PDF", "url": app.MOODLE_URL + "/pluginfile.php/1/a.pdf"}])
        get.assert_called_once_with(app.MOODLE_URL + "/mod/page/view.php?id=1")

    def test_sync_tool_defaults_to_real_preview_without_writing(self):
        root = self.temporary_directory() / "not-created"
        activity = self.activity(1, "resource")
        url = app.MOODLE_URL + "/pluginfile.php/1/mod_resource/content/1/notes.pdf"
        with patch.object(self.client, "course", return_value=IMAGINARY), \
                patch.object(self.client, "activities", return_value=[activity]), \
                patch.object(self.client, "files", return_value=[app.File("notes.pdf", url)]), \
                patch.object(self.client.session, "head", return_value=response(url, headers={"Etag": '"v1"'})) as head, \
                patch.object(self.client.session, "get") as get:
            result = self.call_tool("sync_course", course="Imaginary", dest_dir=str(root))
        self.assertTrue(result["dry_run"])
        # Files are sorted by kind ("notes" -> Slides), not mirrored by Moodle section.
        self.assertEqual(result["new"], {"count": 1, "files": ["Slides/notes.pdf"]})
        self.assertFalse(root.exists())
        head.assert_called_once()
        get.assert_not_called()

    def test_sync_tool_downloads_on_explicit_false_and_second_run_is_unchanged(self):
        root = self.temporary_directory() / "course"
        activity = self.activity(1, "resource")
        url = app.MOODLE_URL + "/pluginfile.php/1/mod_resource/content/1/notes.pdf"
        with patch.object(self.client, "course", return_value=IMAGINARY), \
                patch.object(self.client, "activities", return_value=[activity]), \
                patch.object(self.client, "files", return_value=[app.File("notes.pdf", url)]), \
                patch.object(self.client.session, "head", side_effect=lambda *a, **kw: response(url, headers={"Etag": '"v1"'})), \
                patch.object(self.client.session, "get", side_effect=lambda *a, **kw:
                             response(url, body=b"Synthetic PDF", headers={"Etag": '"v1"'})) as get:
            first = self.call_tool("sync_course", course="Imaginary", dest_dir=str(root), dry_run=False)
            second = self.call_tool("sync_course", course="Imaginary", dest_dir=str(root), dry_run=False)
        self.assertEqual(first["new"]["count"], 1)
        self.assertEqual(second["unchanged"]["count"], 1)
        self.assertEqual((root / "Slides/notes.pdf").read_bytes(), b"Synthetic PDF")
        self.assertTrue((root / ".moodle-sync-7.json").exists())
        self.assertEqual(get.call_count, 1)

    def test_sync_internal_errors_hide_cookie_and_sesskey(self):
        root = self.temporary_directory() / "not-created"
        self.client._sesskey = "synthetic-sync-key"
        self.client.session.cookies.set("MoodleSession", "synthetic-sync-cookie")
        with patch.object(self.client, "course", return_value=IMAGINARY), \
                patch.object(self.client, "activities", return_value=[self.activity(1, "resource")]), \
                patch.object(self.client, "files", side_effect=RuntimeError(
                    "synthetic-sync-cookie sesskey=synthetic-sync-key")):
            result = self.call_tool("sync_course", course="Imaginary", dest_dir=str(root))
        self.assertEqual(result["failed"]["count"], 1)
        self.assertNotIn("synthetic-sync", json.dumps(result))
        self.assertFalse(root.exists())

    def test_save_refuses_existing_file_without_request_and_uses_unique_part(self):
        root = self.temporary_directory()
        target = root / "notes.pdf"
        target.write_bytes(b"Original")
        url = app.MOODLE_URL + "/pluginfile.php/1/notes.pdf"
        with patch.object(self.client, "get") as get:
            with self.assertRaises(app.MoodleError):
                self.client.save(url, target)
        get.assert_not_called()
        self.assertEqual(target.read_bytes(), b"Original")
        legacy_part = root / "new.pdf.part"
        legacy_part.write_bytes(b"Previous partial")
        with patch.object(self.client, "get", return_value=response(url, body=b"New")):
            self.client.save(url, root / "new.pdf")
        self.assertEqual(legacy_part.read_bytes(), b"Previous partial")
        self.assertEqual((root / "new.pdf").read_bytes(), b"New")

    def test_save_racing_destination_is_preserved_and_partial_remains(self):
        root = self.temporary_directory()
        target = root / "notes.pdf"
        url = app.MOODLE_URL + "/pluginfile.php/1/notes.pdf"
        result = response(url)
        def chunks(size):
            yield b"Download"
            target.write_bytes(b"Created concurrently")
        result.iter_content = chunks
        with patch.object(self.client, "get", return_value=result):
            with self.assertRaises(FileExistsError):
                self.client.save(url, target)
        self.assertEqual(target.read_bytes(), b"Created concurrently")
        self.assertEqual(next(root.glob("*.part")).read_bytes(), b"Download")

    def test_direct_download_rejects_split_session_values_before_writing(self):
        root = self.temporary_directory()
        self.client._sesskey = "synthetic-download-secret"
        url = app.MOODLE_URL + "/pluginfile.php/1/notes.pdf"
        result = response(url)
        result.iter_content = lambda size: iter([b"Data synthetic-download-", b"secret end"])
        with patch.object(self.client, "get", return_value=result), \
                patch.object(self.client, "files", return_value=[app.File("notes.pdf", url)]):
            error = self.assert_error(self.call_tool("download", url=url, dest_dir=str(root)))
        self.assertNotIn(self.client._sesskey, error)
        self.assertFalse(list(root.iterdir()))

    def test_download_sanitizes_server_names_without_interpreting_them_as_paths(self):
        root = self.temporary_directory()
        url = app.MOODLE_URL + "/pluginfile.php/1/file.pdf"
        names = ["../escape.pdf", "C:\\outside.pdf", "CON .txt"]
        with patch.object(self.client, "files", return_value=[app.File(name, url) for name in names]), \
                patch.object(self.client, "save", side_effect=lambda file, path: path) as save:
            results = self.call_tool("download", url=url, dest_dir=str(root))
        self.assertEqual(len(results), len(names))
        for call in save.call_args_list:
            self.assertEqual(call.args[1].parent, root)
        self.assertEqual(save.call_args_list[-1].args[1].name, "_CON .txt")

    def test_save_refuses_session_values_in_paths_before_creating_any_files(self):
        root = self.temporary_directory()
        self.client._sesskey = "synthetic-path-secret"
        url = app.MOODLE_URL + "/pluginfile.php/1/file.pdf"
        with patch.object(self.client, "get") as get:
            with self.assertRaises(app.MoodleError):
                self.client.save(url, root / "synthetic-path-secret.pdf")
        get.assert_not_called()
        self.assertFalse(list(root.iterdir()))

    def test_save_accepts_literal_percent_in_ordinary_filename(self):
        root = self.temporary_directory()
        target = root / "progress%20.pdf"
        url = app.MOODLE_URL + "/pluginfile.php/1/file.pdf"
        with patch.object(self.client, "get", return_value=response(url, body=b"Synthetic PDF")):
            self.client.save(url, target)
        self.assertEqual(target.read_bytes(), b"Synthetic PDF")

    def test_direct_download_rejects_split_login_form_before_writing(self):
        root = self.temporary_directory()
        url = app.MOODLE_URL + "/pluginfile.php/1/page.html"
        result = response(url)
        result.iter_content = lambda size: iter([b"<html><for", b"m><input name='j_pass", b"word'></form></html>"])
        with patch.object(self.client, "get", return_value=result), \
                patch.object(self.client, "files", return_value=[app.File("page.html", url)]):
            self.assert_error(self.call_tool("download", url=url, dest_dir=str(root)))
        self.assertFalse(list(root.iterdir()))

    def read_only_tool(self, name):
        return self.owner.build_server(read_only=True)._tool_manager.get_tool(name).fn

    def test_read_only_tools_refuse_sesskey_urls_before_any_request(self):
        read_page = self.read_only_tool("read_page")
        with patch.object(self.client, "page") as page, patch.object(self.client, "files") as files:
            for url in (app.MOODLE_URL + "/login/logout.php?sesskey=abcdefghij",
                        app.MOODLE_URL + "/mod/forum/subscribe.php?id=1&SessKey=x",
                        app.MOODLE_URL + "/mod/forum/subscribe.php?id=1%26sesskey%3Dx"):
                self.assertIn("sesskey", self.assert_error(read_page(url=url)))
                self.assert_error(self.read_only_tool("list_files")(url=url))
            page.assert_not_called()
            files.assert_not_called()

    def test_read_only_results_mask_session_values_and_sesskey_links(self):
        self.client._sesskey = "Imaginary1sesskey"
        self.client.session.cookies.set("MoodleSession", "imaginary-cookie-value-123", domain=app.MOODLE_HOST)
        text = "Token Imaginary1sesskey and imaginary-cookie-value-123 stay hidden; 1,7 stays."
        link = app.MOODLE_URL + "/mod/forum/subscribe.php?id=3&sesskey=OtherValue9"
        page = app.Page(title="Imaginary", text=text, links=[{"text": "Subscribe", "url": link}])
        with patch.object(self.client, "page", return_value=page):
            result = self.read_only_tool("read_page")(url=app.MOODLE_URL + "/mod/page/view.php?id=3")
        dumped = json.dumps(result)
        for secret in ("Imaginary1sesskey", "imaginary-cookie-value-123", "OtherValue9"):
            self.assertNotIn(secret, dumped)
        self.assertIn("1,7 stays", result["text"])
        self.assertEqual(result["links"][0]["url"], app.MOODLE_URL + "/mod/forum/subscribe.php?id=3&sesskey=***")
        # The full MCP server keeps its existing behavior.
        with patch.object(self.client, "page", return_value=page):
            self.assertIn("OtherValue9", json.dumps(self.call_tool("read_page", url=app.MOODLE_URL + "/mod/page/view.php?id=3")))

    def test_read_only_tools_keep_safe_errors(self):
        with patch.object(self.client, "deadlines", side_effect=RuntimeError("boom")):
            self.assertEqual(self.read_only_tool("upcoming_deadlines")(days=3), {"error": "boom"})

    def test_chatgpt_tunnel_passes_key_by_environment_only(self):
        self.no_keyring.side_effect = lambda service, name: {
            "openai_tunnel_id": "tunnel_0123abcd", "openai_tunnel_key": "sk-imaginary-runtime-key"}[name]
        with patch.object(app, "tunnel_client_exe", return_value="C:/tools/tunnel-client.exe"), \
                patch.object(app.subprocess, "call", return_value=0) as call, \
                patch("sys.stdout", new=Mock()):
            self.assertEqual(app.run_chatgpt_tunnel(), 0)
        self.no_keyring.reset_mock()
        command, env = call.call_args.args[0], call.call_args.kwargs["env"]
        self.assertEqual(command[:2], ["C:/tools/tunnel-client.exe", "run"])
        self.assertTrue(command[2].startswith("--mcp.command="))
        self.assertTrue(command[2].endswith("tum_moodle_auto.py mcp-readonly"))
        self.assertNotIn("\\", command[2])
        self.assertNotIn("sk-imaginary-runtime-key", " ".join(command))
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], "sk-imaginary-runtime-key")
        self.assertEqual(env["CONTROL_PLANE_TUNNEL_ID"], "tunnel_0123abcd")

    def test_chatgpt_tunnel_without_stored_key_exits_with_setup_hint(self):
        self.no_keyring.side_effect = lambda service, name: None
        with patch.object(app.subprocess, "call") as call, self.assertRaises(SystemExit) as stop:
            app.run_chatgpt_tunnel()
        self.no_keyring.reset_mock()
        call.assert_not_called()
        self.assertIn("setup-chatgpt", str(stop.exception))

    def test_new_tool_descriptions_state_defaults_and_live_parser_limit(self):
        assignment = self.server._tool_manager.get_tool("assignments").description
        sync = self.server._tool_manager.get_tool("sync_course").description
        self.assertIn("not been verified", assignment)
        self.assertIn("dry_run=true", sync)
        self.assertIn("dry_run=false", sync)
        self.assertIn("never delete", sync)


if __name__ == "__main__":
    unittest.main(verbosity=2)
