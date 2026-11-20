"""Offline transfer recovery, source boundaries and API-contract checks."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "tum_moodle_auto", Path(__file__).resolve().parents[1] / "tum_moodle_auto.py")
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)
artemis = app.artemis


def response(url, status=200, body=b"file contents", **headers):
    result = app.requests.Response()
    result.url, result.status_code = url, status
    result.headers.update(headers)
    result._content, result._content_consumed = body, True
    result.encoding = "utf-8"
    return result


class ClientSyncTests(unittest.TestCase):
    def setUp(self):
        self.blocked = [self.enterContext(patch(name, side_effect=AssertionError("offline only")))
                        for name in ("requests.adapters.HTTPAdapter.send", "socket.socket.connect",
                                     "socket.socket.connect_ex")]
        self.blocked.append(self.enterContext(patch.object(
            app.keyring, "get_password", side_effect=AssertionError("no credential reads"))))
        self.moodle = app.make_client()
        self.artemis = artemis.TumArtemis(lambda: (_ for _ in ()).throw(AssertionError("no credentials")))
        self.artemis.session.cookies.set("jwt", "synthetic-current-token", domain=artemis.ARTEMIS_HOST)
        self.addCleanup(self.moodle.session.close)
        self.addCleanup(self.artemis.session.close)
        self.sleep = self.enterContext(patch.object(artemis.time, "sleep"))

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()

    def clients(self):
        return ((self.moodle, app.MOODLE_URL + "/pluginfile.php/7/a.pdf"),
                (self.artemis, artemis.ARTEMIS_URL + "/api/core/files/attachments/lecture/7/a.pdf"))

    def test_transient_retries_are_bounded_and_retry_after_is_capped(self):
        for client, url in self.clients():
            with self.subTest(client=type(client).__name__):
                answers = [app.requests.Timeout("synthetic timeout"),
                           response(url, 429, **{"Retry-After": "999999"}), response(url)]
                with patch.object(client.session, "get", side_effect=answers) as get, \
                        patch.object(client, "login") as login:
                    self.assertEqual(client.sync_request("GET", url).content, b"file contents")
                    self.assertEqual(get.call_count, 3)
                    login.assert_not_called()
                self.assertTrue(all(call.args[0] <= 2 for call in self.sleep.call_args_list))
                with patch.object(client.session, "head", side_effect=lambda *a, **k: response(url, 503)) as head, \
                        patch.object(client, "login") as login:
                    with self.assertRaises(app.requests.HTTPError):
                        client.sync_request("HEAD", url)
                    self.assertEqual(head.call_count, 3)
                    login.assert_not_called()

    def test_head_recovers_authentication_once_and_403_never_logs_in(self):
        for client, url in self.clients():
            with self.subTest(client=type(client).__name__):
                with patch.object(client.session, "head", side_effect=[response(url, 401), response(url)]) as head, \
                        patch.object(client, "login") as login:
                    client.sync_request("HEAD", url)
                    self.assertEqual(head.call_count, 2)
                    login.assert_called_once()
                with patch.object(client.session, "head", return_value=response(url, 403)) as head, \
                        patch.object(client, "login") as login:
                    with self.assertRaises(app.requests.HTTPError):
                        client.sync_request("HEAD", url)
                    head.assert_called_once()
                    login.assert_not_called()
                with patch.object(client.session, "head", return_value=response(url, 401)) as head, \
                        patch.object(client, "login", side_effect=app.NotLoggedIn("password refused")) as login:
                    with self.assertRaises(app.NotLoggedIn):
                        client.sync_request("HEAD", url)
                    head.assert_called_once()
                    login.assert_called_once()

    def test_stream_login_form_recovers_in_memory_and_replays_safe_bytes(self):
        form = b"<html><form action='/login/index.php'><input name='logintoken'></form>"
        for client, url in self.clients():
            with self.subTest(client=type(client).__name__):
                bad = response(url, body=form)
                bad.iter_content = Mock(return_value=iter([form[:15], form[15:]]))
                good = response(url, body=b"%PDF safe contents")
                with patch.object(client.session, "get", side_effect=[bad, good]), \
                        patch.object(client, "login") as login:
                    result = client.sync_request("GET", url, stream=True)
                    data = b"".join(app.moodle_sync.safe_download_chunks(result, client))
                    self.assertEqual(data, b"%PDF safe contents")
                    login.assert_called_once()
                with patch.object(client.session, "get", side_effect=lambda *a, **k: response(url, body=form)) as get, \
                        patch.object(client, "login") as login:
                    with self.assertRaises(app.NotLoggedIn):
                        client.sync_request("GET", url, stream=True)
                    self.assertEqual(get.call_count, 2)
                    login.assert_called_once()

    def test_external_redirect_and_non_read_methods_stop_before_followup(self):
        for client, url in self.clients():
            with self.subTest(client=type(client).__name__):
                with patch.object(client.session, "get", return_value=response(
                        url, 302, Location="https://outside.example/file")) as get, \
                        patch.object(client, "login") as login:
                    with self.assertRaises(app.MoodleError):
                        client.sync_request("GET", url, allow_redirects=True)
                    get.assert_called_once()
                    login.assert_not_called()
                with patch.object(client.session, "get") as get:
                    with self.assertRaises(app.MoodleError):
                        client.sync_request("POST", url)
                    get.assert_not_called()

    def test_moodle_login_redirect_is_intercepted_before_following(self):
        url = app.MOODLE_URL + "/pluginfile.php/7/a.pdf"
        with patch.object(self.moodle.session, "get", side_effect=[
                response(url, 302, Location="/login/index.php"), response(url)]) as get, \
                patch.object(self.moodle, "login") as login:
            self.moodle.sync_request("GET", url)
            self.assertEqual([c.args[0] for c in get.call_args_list], [url, url])
            login.assert_called_once()

    def test_explicit_password_refusal_stops_later_transfers_until_restart(self):
        self.artemis.session.cookies.clear()
        self.artemis._credentials = lambda: ("ab12cde", "synthetic-password", None)
        with patch.object(self.artemis.session, "post", return_value=response(
                artemis.ARTEMIS_URL + artemis.LOGIN_PATH, 401)) as post, \
                patch.object(self.artemis.session, "head") as head:
            for _ in range(2):
                with self.assertRaises(artemis.CredentialsRejected):
                    self.artemis.sync_request("HEAD", artemis.ARTEMIS_URL + "/api/core/files/attachments/a")
            post.assert_called_once()
            head.assert_not_called()
        url = app.MOODLE_URL + "/pluginfile.php/1/a.pdf"
        with patch.object(self.moodle.session, "head", return_value=response(url, 401)) as head, \
                patch.object(app.TumSso, "login", side_effect=artemis.CredentialsRejected("password refused")) as sso:
            for _ in range(2):
                with self.assertRaises(artemis.CredentialsRejected):
                    self.moodle.sync_request("HEAD", url)
            head.assert_called_once()
            sso.assert_called_once()

    def test_unknown_mfa_and_login_network_failure_are_not_permanently_cached(self):
        for failure in (app.NotLoggedIn("manual MFA needed"), app.requests.Timeout("login network failure")):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(app.TumSso, "login", side_effect=failure) as sso:
                for _ in range(2):
                    with self.assertRaises(type(failure)):
                        self.moodle.login()
                self.assertEqual(sso.call_count, 2)
                self.assertFalse(self.moodle._credentials_rejected)

    def test_safe_internal_file_redirect_is_validated_and_followed(self):
        url = app.MOODLE_URL + "/mod/resource/view.php?id=7"
        with patch.object(self.moodle.session, "head", side_effect=[
                response(url, 302, Location="/pluginfile.php/7/a.pdf"),
                response(app.MOODLE_URL + "/pluginfile.php/7/a.pdf")]) as head:
            self.moodle.sync_request("HEAD", url)
            self.assertEqual(head.call_count, 2)
            self.assertFalse(head.call_args.kwargs["allow_redirects"])

    def test_page_and_label_files_stay_inside_the_correct_content(self):
        source = """<title>Synthetic</title><div id='region-main'>
        <li id='module-7' class='activity label'><div class='activity-altcontent'>
        <p>Read Chapter 2</p><a href='/pluginfile.php/7/notes.pdf'>Notes</a>
        <a href='/mod/page/view.php?id=99'>Another activity</a>
        <a href='https://outside.example/pluginfile.php/x.pdf'>External</a>
        <a href='/pluginfile.php/7/private.pdf?sesskey=synthetic'>Sensitive</a>
        </div></li><li id='module-8'><a href='/pluginfile.php/8/other.pdf'>Other module</a></li>
        </div>"""
        label = app.Activity(7, "Instructions", "label", None, "Intro", 0, True, 42)
        with patch.object(self.moodle, "get", return_value=response("", body=source.encode())) as get:
            files = self.moodle.files(label)
            self.assertEqual([f.name for f in files], ["notes.pdf"])
            self.assertEqual(get.call_args.args[0], app.MOODLE_URL + "/course/view.php?id=42")
            snapshot = self.moodle.sync_text(label)
            self.assertIn("Read Chapter 2", snapshot["text"])
            self.assertNotIn("Other module", snapshot["text"])
            self.assertIn("[7]", snapshot["filename"])
        missing = app.Activity(999, "Missing", "label", None, "Intro", 0, True, 42)
        with patch.object(self.moodle, "get", return_value=response("", body=source.encode())):
            with self.assertRaises(app.MoodleError):
                self.moodle.files(missing)

    def test_page_snapshot_removes_ui_and_session_values(self):
        secret = "synthetic-private-session"
        self.moodle._sesskey = secret
        page = app.Activity(9, "Reading", "page", app.MOODLE_URL + "/mod/page/view.php?id=9&sesskey=" + secret,
                            "Intro", 0, True, 42)
        source = ("<div id='region-main'><h2>Reading</h2><p>Useful table</p>"
                  "<script>secret script</script><nav>Navigation</nav><div class='action-menu'>Edit</div>"
                  "<p>https://www.moodle.tum.de/course/view.php?id=42&amp;sesskey=" + secret + "</p></div>")
        with patch.object(self.moodle, "get", return_value=response("", body=source.encode())):
            snapshot = self.moodle.sync_text(page)
        self.assertIn("Useful table", snapshot["text"])
        for forbidden in (secret, "sesskey", "Navigation", "secret script", "Edit"):
            self.assertNotIn(forbidden, str(snapshot))


class ArtemisContractTests(unittest.TestCase):
    def test_malformed_payloads_do_not_become_empty_courses_or_lectures(self):
        for payload in ({}, {"courseList": []}, {"courses": [{"renamedId": 1}]}, {"courses": "changed"}):
            with self.subTest(payload=payload), self.assertRaises(app.MoodleError):
                artemis.parse_courses(payload)
        self.assertEqual(artemis.parse_courses({"courses": []}), [])
        for payload in ({}, ["invalid"], [{"id": 1}], [{"id": 1, "lectureUnits": {}}],
                        [{"id": 1, "lectureUnits": [{"id": 2, "attachment": "changed"}]}]):
            with self.subTest(payload=payload), self.assertRaises(app.MoodleError):
                artemis.parse_lectures(payload, 1)
        self.assertEqual(artemis.parse_lectures([], 1), [])

    def test_exercise_absence_remains_legal_but_wrong_types_fail(self):
        self.assertEqual(artemis.parse_exercises({"totalScores": {}}, 1), [])
        for payload in ({"exercises": {}}, {"exercises": None}, {"error": "upstream failure"},
                        {"exercises": [{"renamedId": 1}]}, {"participationResults": {}},
                        {"exercises": [{"id": 1, "studentParticipations": {}}]}):
            with self.subTest(payload=payload), self.assertRaises(app.MoodleError):
                artemis.parse_exercises(payload, 1)


if __name__ == "__main__":
    unittest.main()
