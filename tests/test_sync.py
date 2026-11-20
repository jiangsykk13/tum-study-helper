"""Synthetic, network-free synchronization tests; all files use system tempfile."""

import importlib.util
import hashlib
import json
import os
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "moodle_sync", Path(__file__).resolve().parents[1] / "moodle_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

BASE = "https://www.moodle.tum.de"
MODIFIED = "Mon, 12 Oct 2026 10:00:00 GMT"
COOKIE = "synthetic-cookie-value"
SESSKEY = "synthetic-sesskey-value"


def activity(cmid=10, name="Notes", section="Week 1", kind="resource", available=True, course_id=7):
    return SimpleNamespace(id=cmid, name=name, section=section, type=kind,
                           section_number=1, available=available, course_id=course_id,
                           url=f"{BASE}/mod/{kind}/view.php?id={cmid}")


def file(name="notes.pdf", cmid=10, version=1, folder="", kind="resource"):
    folders = "/".join(quote(part, safe="") for part in folder.split("/") if part)
    relative = (folders + "/" if folders else "") + quote(name, safe="")
    return SimpleNamespace(name=name,
                           url=f"{BASE}/pluginfile.php/{cmid}/mod_{kind}/content/{version}/{relative}")


class FakeResponse:
    def __init__(self, url, record, head=False):
        self.url = record.get("response_url", url)
        etag = record.get("etag", '"v1"')
        if not head and "get_etag" in record:
            etag = record["get_etag"]  # A compressing server changes the GET Etag only.
        self.headers = {"Etag": etag,
                        "Last-Modified": record.get("last_modified", MODIFIED),
                        "Content-Type": record.get("content_type", "application/pdf")}
        self.headers = {key: value for key, value in self.headers.items() if value is not None}
        self.status = record.get("head_status" if head else "get_status", 200)
        self.record = record
        self.closed = False
        self.body_reads = 0
        self.history = []

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"synthetic failure {COOKIE} sesskey={SESSKEY}")

    def iter_content(self, size):
        self.body_reads += 1
        for chunk in self.record.get("chunks", [self.record.get("body", b"PDF version one")]):
            yield chunk
        if self.record.get("stream_error"):
            raise RuntimeError(f"synthetic stream failure {COOKIE} sesskey={SESSKEY}")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.responses = []
        self.cookies = [SimpleNamespace(value=COOKIE)]

    def _response(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        record = self.records.setdefault(url, {})
        if record.get(method.lower() + "_error"):
            raise RuntimeError(f"synthetic request failure {COOKIE} sesskey={SESSKEY}")
        result = FakeResponse(url, record, method == "HEAD")
        self.responses.append(result)
        return result

    def head(self, url, **kwargs):
        return self._response("HEAD", url, kwargs)

    def get(self, url, **kwargs):
        return self._response("GET", url, kwargs)


class FakeClient:
    def __init__(self, activities=None, files=None):
        self.items = activities if activities is not None else [activity()]
        self.by_cmid = files if files is not None else {10: [file()]}
        self.session = FakeSession()
        self._sesskey = SESSKEY
        self.timeout = 12
        self.activities_calls = []
        self.files_calls = []

    def activities(self, course):
        self.activities_calls.append(course)
        return self.items

    def files(self, target):
        self.files_calls.append(target.id)
        result = self.by_cmid.get(target.id, [])
        if isinstance(result, Exception):
            raise result
        return result


class FakeBin:
    """Stand-in for the Windows Recycle Bin: remembers each trashed file's name and bytes."""

    def __init__(self):
        self.items = []

    def __call__(self, path):
        path = Path(path)
        self.items.append((path.name, path.read_bytes()))
        path.unlink()


class SyncTests(unittest.TestCase):
    def setUp(self):
        import keyring
        self.bin = FakeBin()
        self.enterContext(patch.object(sync, "_trash", self.bin))
        self.blocked = [
            self.enterContext(patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("No HTTP"))),
            self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network"))),
            self.enterContext(patch("socket.socket.connect_ex", side_effect=AssertionError("No network"))),
            self.enterContext(patch.object(keyring, "get_password", side_effect=AssertionError("No credentials"))),
            self.enterContext(patch.object(keyring, "set_password", side_effect=AssertionError("No credentials"))),
        ]
        # Python 3.12's Windows mkdir(0o700) grants only the account SID; the
        # restricted sandbox SID then cannot use its own test directory. Retain
        # the system Temp directory's existing inherited permissions for these
        # entirely synthetic fixtures, and restore mkdir immediately afterward.
        saved_mkdir = os.mkdir
        with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kwargs:
                   saved_mkdir(path, 0o777, **kwargs)):
            temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "download"
        self.client = FakeClient()

    def tearDown(self):
        for method in self.blocked:
            method.assert_not_called()

    def run_sync(self, dry_run=False, course="7"):
        return sync.sync_course(self.client, course, self.root, dry_run=dry_run)

    def state(self):
        return json.loads((self.root / sync.STATE_NAME).read_text(encoding="utf-8"))

    def target(self, result, kind="new"):
        return self.root / result[kind]["files"][0]

    def snapshot(self):
        if not self.root.exists():
            return None
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_first_sync_state_headers_identity_and_mtime(self):
        result = self.run_sync()
        self.assertEqual(result["new"], {"count": 1, "files": ["Week 1/notes.pdf"]})
        self.assertEqual(result["failed"]["count"], 0)
        target = self.target(result)
        self.assertEqual(target.read_bytes(), b"PDF version one")
        self.assertEqual(target.stat().st_mtime, datetime(2026, 10, 12, 10, tzinfo=timezone.utc).timestamp())
        state = self.state()
        entry = next(iter(state["files"].values()))
        self.assertEqual(state["version"], 2)
        self.assertEqual(json.loads(next(iter(state["files"]))), [7, 10, "", "notes.pdf"])
        self.assertEqual(entry["etag"], '"v1"')
        self.assertEqual(entry["last_modified"], MODIFIED)
        self.assertTrue(entry["last_sync"])
        self.assertFalse(list(self.root.rglob("*.part")))
        self.assertTrue(all(response.closed for response in self.client.session.responses))

    def test_second_sync_is_unchanged_without_download(self):
        self.run_sync()
        self.client.session.calls.clear()
        result = self.run_sync()
        self.assertEqual(result["unchanged"]["count"], 1)
        self.assertEqual(result["new"]["count"], 0)
        self.assertEqual([call[0] for call in self.client.session.calls], ["HEAD"])

    def test_changed_etag_preserves_old_version_and_conflicting_backup(self):
        first = self.run_sync()
        target = self.target(first)
        backup = target.with_name("notes_old.pdf")
        backup.write_bytes(b"unmanaged previous backup")
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"new version"}
        result = self.run_sync()
        self.assertEqual(result["updated"]["count"], 1)
        self.assertEqual(target.read_bytes(), b"new version")
        # The old version goes to the Recycle Bin; an untracked file with its name stays.
        self.assertEqual(backup.read_bytes(), b"unmanaged previous backup")
        self.assertFalse(target.with_name("notes_old (1).pdf").exists())
        self.assertEqual(self.bin.items, [("notes_old (1).pdf", b"PDF version one")])
        self.assertEqual(result["trashed"]["files"], ["Week 1/notes_old (1).pdf"])
        self.assertEqual(result["artifacts"][0]["history"], [])
        self.assertEqual(next(iter(self.state()["files"].values()))["history"], [])

    def test_resource_url_version_alone_triggers_update(self):
        self.run_sync()
        changed = file(version=2)
        self.client.by_cmid[10] = [changed]
        self.client.session.records[changed.url] = {"body": b"version two same headers"}
        result = self.run_sync()
        self.assertEqual(result["updated"]["count"], 1)
        self.assertEqual(self.bin.items, [("notes_old.pdf", b"PDF version one")])

    def test_metadata_change_with_identical_content_does_not_keep_duplicate_old(self):
        self.run_sync()
        self.client.session.records[file().url] = {"last_modified": "Mon, 12 Oct 2026 11:00:00 GMT"}
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)
        self.assertFalse(list(self.root.rglob("*_old*")))
        self.assertEqual(next(iter(self.state()["files"].values()))["last_modified"],
                         "Mon, 12 Oct 2026 11:00:00 GMT")

    def test_online_deletion_moves_local_file_to_recycle_bin(self):
        first = self.run_sync()
        self.client.by_cmid[10] = []
        preview = self.run_sync(dry_run=True)
        self.assertEqual(preview["removed_online"], {"count": 1, "files": ["Week 1/notes.pdf"]})
        self.assertTrue(self.target(first).exists())  # A preview never trashes.
        result = self.run_sync()
        self.assertEqual(result["removed_online"], {"count": 1, "files": ["Week 1/notes.pdf"]})
        self.assertEqual(result["trashed"], {"count": 1, "files": ["Week 1/notes.pdf"]})
        self.assertFalse(self.target(first).exists())
        self.assertEqual(self.bin.items, [("notes.pdf", b"PDF version one")])
        self.assertEqual(self.state()["files"], {})
        self.assertEqual(self.run_sync()["removed_online"]["count"], 0)

    def test_removed_activity_is_trashed_but_an_empty_course_is_not(self):
        self.run_sync()
        self.client.items = []
        result = self.run_sync()  # Every activity gone at once looks like a glitch.
        self.assertEqual(result["removed_online"]["count"], 0)
        self.assertTrue((self.root / "Week 1/notes.pdf").exists())
        self.client.items = [activity(11, name="Other")]
        self.client.by_cmid[11] = [file("other.pdf", cmid=11)]
        result = self.run_sync()
        self.assertEqual(result["removed_online"]["files"], ["Week 1/notes.pdf"])
        self.assertFalse((self.root / "Week 1/notes.pdf").exists())

    def test_removed_file_the_user_changed_is_kept_untracked(self):
        first = self.run_sync()
        self.target(first).write_bytes(b"my annotations")
        self.client.by_cmid[10] = []
        result = self.run_sync()
        self.assertEqual(result["kept_local"]["files"], ["Week 1/notes.pdf"])
        self.assertEqual(self.target(first).read_bytes(), b"my annotations")
        self.assertEqual(self.bin.items, [])
        self.assertEqual(self.state()["files"], {})

    def test_recycle_bin_failure_keeps_file_and_record_for_next_time(self):
        self.run_sync()
        self.client.by_cmid[10] = []
        with patch.object(sync, "_trash", side_effect=sync.SyncError("locked", code="trash_failed", retryable=True)):
            result = self.run_sync()
        self.assertEqual(result["errors"][0]["code"], "trash_failed")
        self.assertTrue((self.root / "Week 1/notes.pdf").exists())
        self.assertEqual(len(self.state()["files"]), 1)
        self.assertEqual(self.run_sync()["trashed"]["count"], 1)

    def test_existing_old_versions_are_trashed_on_next_real_sync(self):
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"two"}
        self.run_sync()
        self.client.session.records[file().url] = {"etag": '"v3"', "body": b"three"}
        with patch.object(sync, "_trash", side_effect=sync.SyncError("locked", code="trash_failed")):
            self.run_sync()  # As before this change: an _old version stays on disk and in history.
        self.assertTrue((self.root / "Week 1/notes_old.pdf").exists())
        self.assertEqual(self.run_sync(dry_run=True)["trashed"]["count"], 0)
        result = self.run_sync()
        self.assertEqual(result["trashed"]["files"], ["Week 1/notes_old.pdf"])
        self.assertEqual(self.bin.items, [("notes_old.pdf", b"two")])

    def test_local_change_and_online_update_keep_both(self):
        first = self.run_sync()
        self.target(first).write_bytes(b"my annotations")
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"new version"}
        preview = self.run_sync(dry_run=True)
        self.assertEqual(preview["kept_local"]["files"], ["Week 1/notes (批注).pdf"])
        self.assertEqual(preview["updated"]["files"], ["Week 1/notes.pdf"])
        result = self.run_sync()
        self.assertEqual((result["kept_local"], result["updated"]), (preview["kept_local"], preview["updated"]))
        self.assertEqual((self.root / "Week 1/notes (批注).pdf").read_bytes(), b"my annotations")
        self.assertEqual(self.target(first).read_bytes(), b"new version")
        self.assertEqual((self.bin.items, result["errors"]), ([], []))
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)

    def test_single_failure_continues_and_error_is_redacted(self):
        second = file("second.pdf", cmid=11)
        self.client.items.append(activity(11))
        self.client.by_cmid[11] = [second]
        self.client.session.records[file().url] = {"get_error": True}
        result = self.run_sync()
        self.assertEqual(result["new"]["count"], 1)
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual((self.root / "Week 1/second.pdf").read_bytes(), b"PDF version one")
        self.assertNotIn(COOKIE, json.dumps(result))
        self.assertNotIn(SESSKEY, json.dumps(result))

    def test_failed_stream_keeps_original_and_partial_download(self):
        self.run_sync()
        state_before = (self.root / sync.STATE_NAME).read_bytes()
        partial = b"partial" * 10000
        self.client.session.records[file().url] = {"etag": '"v2"', "body": partial, "stream_error": True}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), b"PDF version one")
        self.assertFalse((self.root / "Week 1/notes_old.pdf").exists())
        self.assertEqual(next(self.root.rglob("*.part")).read_bytes(), partial)
        self.assertEqual((self.root / sync.STATE_NAME).read_bytes(), state_before)

    def test_failed_commit_restores_original_without_overwriting(self):
        self.run_sync()
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"new version"}
        real_move = sync._move_no_replace
        moves = []
        def fail_install(source, target):
            moves.append((source, target))
            if target.name == "notes.pdf" and source.name.endswith(".part"):
                raise OSError("synthetic commit failure")
            return real_move(source, target)
        with patch.object(sync, "_move_no_replace", side_effect=fail_install):
            result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), b"PDF version one")
        self.assertEqual(next(self.root.rglob("*.part")).read_bytes(), b"new version")
        self.assertEqual(len(moves), 4)  # journal, backup, failed install, rollback

    def test_initial_dry_run_creates_nothing_and_never_reads_bodies(self):
        result = self.run_sync(dry_run=True)
        self.assertEqual(result["new"]["count"], 1)
        self.assertFalse(self.root.exists())
        self.assertEqual([call[0] for call in self.client.session.calls], ["HEAD"])
        self.assertTrue(all(response.body_reads == 0 for response in self.client.session.responses))

    def test_update_dry_run_neither_renames_nor_writes_state(self):
        self.run_sync()
        before = self.snapshot()
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"new version"}
        result = self.run_sync(dry_run=True)
        self.assertEqual(result["updated"]["count"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_windows_invalid_and_reserved_names_are_safe(self):
        names = ["CON.txt", 'bad<>:"/\\|?*.pdf', "trailing. ", "NUL", "com1.TXT"]
        self.client.by_cmid[10] = [file(name) for name in names]
        result = self.run_sync()
        self.assertEqual(result["new"]["count"], len(names))
        self.assertEqual(result["failed"]["count"], 0)
        for relative in result["new"]["files"]:
            name = Path(relative).name
            self.assertEqual(sync.safe_name(name), name)
            self.assertTrue((self.root / relative).is_file())
        self.assertIn("Week 1/_CON.txt", result["new"]["files"])

    def test_folder_preserves_nested_paths_and_sanitizes_traversal(self):
        self.client.items = [activity(kind="folder", name="Reading / Notes")]
        self.client.by_cmid[10] = [file("notes.pdf", kind="folder", folder="Unit 1/Examples"),
                                  file("escaped.pdf", kind="folder", folder="../NUL")]
        result = self.run_sync()
        self.assertEqual(result["new"]["count"], 2)
        self.assertIn("Week 1/Reading _ Notes/Unit 1/Examples/notes.pdf", result["new"]["files"])
        self.assertIn("Week 1/Reading _ Notes/_/_NUL/escaped.pdf", result["new"]["files"])
        identities = [json.loads(value) for value in self.state()["files"]]
        self.assertIn([7, 10, "Unit 1/Examples", "notes.pdf"], identities)

    def test_reserved_windows_names_with_spaces_before_extension_are_sanitized(self):
        names = ["CON .txt", "NUL .pdf", "COM1 .pdf", "LPT9 .txt"]
        for name in names:
            self.assertTrue(PureWindowsPath(name).is_reserved())
            self.assertFalse(PureWindowsPath(sync.safe_name(name)).is_reserved())
        self.client.by_cmid[10] = [file(name) for name in names]
        result = self.run_sync()
        self.assertEqual(result["new"]["count"], 4)
        self.assertEqual(result["failed"]["count"], 0)
        for relative in result["new"]["files"]:
            self.assertFalse(PureWindowsPath(relative).is_reserved())
            self.assertTrue((self.root / relative).is_file())

    def test_case_insensitive_name_collision_and_unmanaged_file_are_preserved(self):
        self.root.mkdir()
        (self.root / "Week 1").mkdir()
        unmanaged = self.root / "Week 1/NOTES.pdf"
        unmanaged.write_bytes(b"user notes")
        self.client.items.append(activity(11))
        self.client.by_cmid[11] = [file("Notes.pdf", cmid=11)]
        result = self.run_sync()
        self.assertEqual(result["new"]["count"], 2)
        self.assertEqual(unmanaged.read_bytes(), b"user notes")
        self.assertEqual(len({path.casefold() for path in result["new"]["files"]}), 2)
        self.assertTrue(all(path.casefold() != "week 1/notes.pdf" for path in result["new"]["files"]))

    def test_long_folder_paths_are_shortened_and_can_update(self):
        self.client.items = [activity(kind="folder", name="Folder" * 30, section="Section" * 30)]
        first_file = file("LongFilename" * 20 + ".pdf", kind="folder", folder="/".join(["nested" * 30] * 6))
        self.client.by_cmid[10] = [first_file]
        first = self.run_sync()
        self.assertEqual(first["new"]["count"], 1)
        self.assertLessEqual(sync._units(str(self.target(first))), sync.BASE_PATH_UNITS)
        self.client.session.records[first_file.url] = {"etag": '"v2"', "body": b"changed nested file"}
        second = self.run_sync()
        self.assertEqual(second["updated"]["count"], 1)
        self.assertTrue(any("_old" in name for name, _ in self.bin.items))

    def test_corrupt_unknown_or_duplicate_key_state_is_never_overwritten(self):
        self.root.mkdir()
        path = self.root / sync.STATE_NAME
        invalid = [b"{bad", b'{"version":999,"course_id":7,"files":{}}',
                   b'{"version":1,"version":1,"course_id":7,"files":{}}',
                   b'{"version":1,"course_id":7,"files":[],"other":"user data"}']
        for content in invalid:
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(sync.SyncError):
                    self.run_sync()
                self.assertEqual(path.read_bytes(), content)
        self.assertFalse(self.client.activities_calls)
        self.assertFalse(self.client.session.calls)

    def test_state_path_traversal_and_case_duplicates_are_rejected(self):
        self.run_sync()
        state = self.state()
        key = next(iter(state["files"]))
        path = self.root / sync.STATE_NAME
        for bad_path in ("../../outside.pdf", "C:/outside.pdf", "Week 1\\notes.pdf"):
            with self.subTest(path=bad_path):
                state["files"][key]["local_path"] = bad_path
                path.write_text(json.dumps(state), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaises(sync.SyncError):
                    self.run_sync()
                self.assertEqual(path.read_bytes(), before)
        state["files"][key]["local_path"] = "Week 1/notes.pdf"
        duplicate = dict(state["files"][key], cmid=11, local_path="Week 1/NOTES.pdf")
        state["files"][sync._identity(7, 11, "", "notes.pdf")] = duplicate
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            self.run_sync()

    def test_link_in_state_path_is_rejected_before_requests(self):
        self.run_sync()
        before = self.snapshot()
        real_check = sync._is_link
        with patch.object(sync, "_is_link", side_effect=lambda path: path == self.root / "Week 1" or real_check(path)):
            with self.assertRaises(sync.SyncError):
                self.run_sync()
        self.assertEqual(self.snapshot(), before)

    def test_head_failure_falls_back_to_get_headers_and_closes_without_body(self):
        self.client.session.records[file().url] = {"head_status": 405}
        result = self.run_sync(dry_run=True)
        self.assertEqual(result["new"]["count"], 1)
        self.assertEqual([call[0] for call in self.client.session.calls], ["HEAD", "GET"])
        self.assertTrue(self.client.session.calls[1][2]["stream"])
        self.assertTrue(all(response.closed and response.body_reads == 0
                            for response in self.client.session.responses))

    def test_metadata_absent_does_not_require_content_length(self):
        self.client.session.records[file().url] = {"etag": None, "last_modified": None}
        self.assertEqual(self.run_sync()["new"]["count"], 1)
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)
        entry = next(iter(self.state()["files"].values()))
        self.assertIsNone(entry["etag"])
        self.assertIsNone(entry["last_modified"])

    def test_failed_file_listing_does_not_claim_online_deletion(self):
        self.run_sync()
        self.client.by_cmid[10] = RuntimeError(f"synthetic {COOKIE}")
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual(result["removed_online"]["count"], 0)
        self.assertEqual(len(self.state()["files"]), 1)

    def test_unavailable_and_other_activity_types_are_not_requested_or_removed(self):
        self.run_sync()
        self.client.items[0].available = False
        self.client.items.append(activity(11, kind="assign"))
        self.client.files_calls.clear()
        result = self.run_sync()
        self.assertEqual(result["removed_online"]["count"], 0)
        self.assertEqual(result["new"]["count"], 0)
        self.assertFalse(self.client.files_calls)

    def test_unsafe_and_session_bearing_urls_are_rejected_without_requests(self):
        invalid = ["https://outside.example/pluginfile.php/file.pdf",
                   "http://www.moodle.tum.de/pluginfile.php/file.pdf",
                   file().url + f"?sesskey={SESSKEY}", file().url + "?%73esskey=unknown-value"]
        for url in invalid:
            with self.subTest(url=url):
                self.client.by_cmid[10] = [SimpleNamespace(name="notes.pdf", url=url)]
                result = self.run_sync(dry_run=True)
                self.assertEqual(result["failed"]["count"], 1)
                self.assertNotIn(SESSKEY, json.dumps(result))
        self.assertFalse(self.client.session.calls)
        self.assertFalse(self.root.exists())

    def test_header_and_filename_session_values_are_not_persisted(self):
        self.client.session.records[file().url] = {"etag": COOKIE}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertNotIn(COOKIE, (self.root / sync.STATE_NAME).read_text(encoding="utf-8"))
        self.client.by_cmid[10] = [file(COOKIE + ".pdf")]
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual(result["failed"]["files"], ["[文件路径已隐藏]"])
        self.assertNotIn(COOKIE, json.dumps(result))
        self.assertNotIn(COOKIE, (self.root / sync.STATE_NAME).read_text(encoding="utf-8"))

    def test_encoded_cookie_variants_are_rejected_in_metadata(self):
        self.client.session.cookies = [SimpleNamespace(value="encoded%2Fcookie-value")]
        self.client.session.records[file().url] = {"etag": "encoded/cookie-value"}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertNotIn("encoded/cookie-value", (self.root / sync.STATE_NAME).read_text(encoding="utf-8"))

    def test_login_redirect_or_html_is_never_saved_as_a_file(self):
        cases = [{"response_url": BASE + "/login/index.php"},
                 {"content_type": "text/html", "body": b"<html>Login</html>"}]
        for record in cases:
            with self.subTest(record=record):
                self.client.session.records[file().url] = record
                result = self.run_sync()
                self.assertEqual(result["failed"]["count"], 1)
                self.assertFalse((self.root / "Week 1/notes.pdf").exists())
                self.assertFalse(list(self.root.rglob("*.part")))

    def test_login_form_in_html_file_is_rejected_while_real_html_is_supported(self):
        html_file = file("page.html")
        self.client.by_cmid[10] = [html_file]
        self.client.session.records[html_file.url] = {"content_type": "text/html",
            "body": b"<html><form><input name='j_password'></form></html>"}
        self.assertEqual(self.run_sync()["failed"]["count"], 1)
        self.assertFalse((self.root / "Week 1/page.html").exists())
        self.client.session.records[html_file.url]["body"] = b"<html>Lecture handout</html>"
        self.assertEqual(self.run_sync()["new"]["count"], 1)

    def test_login_form_split_across_chunks_is_rejected_before_part_creation(self):
        html_file = file("page.html")
        self.client.by_cmid[10] = [html_file]
        cases = [[b"<html><fo", b"rm><input name='j_pass", b"word'></form></html>"],
                 [b"<html><form><input name='login", b"token'></form></html>"]]
        for chunks in cases:
            with self.subTest(chunks=chunks):
                self.client.session.records[html_file.url] = {"content_type": "text/html", "chunks": chunks}
                result = self.run_sync()
                self.assertEqual(result["failed"]["count"], 1)
                self.assertFalse((self.root / "Week 1/page.html").exists())
                self.assertFalse(list(self.root.rglob("*.part")))

    def test_session_values_split_across_chunks_are_never_written(self):
        values = [COOKIE.encode(), SESSKEY.encode(),
                  "".join(f"%{ord(char):02x}" for char in COOKIE).encode()]
        for sensitive in values:
            with self.subTest(sensitive=sensitive):
                cut = len(sensitive) // 2
                self.client.session.records[file().url] = {
                    "chunks": [b"file prefix ", sensitive[:cut], sensitive[cut:], b" suffix"]}
                result = self.run_sync()
                self.assertEqual(result["failed"]["count"], 1)
                self.assertFalse((self.root / "Week 1/notes.pdf").exists())
                self.assertFalse(list(self.root.rglob("*.part")))
                self.assertNotIn(COOKIE, json.dumps(result))
                self.assertNotIn(SESSKEY, json.dumps(result))

    def test_late_split_secret_failure_keeps_only_safe_partial_bytes(self):
        self.run_sync()
        secret = COOKIE.encode()
        cut = len(secret) // 2
        self.client.session.records[file().url] = {"etag": '"v2"',
            "chunks": [b"A" * 65536, b"safe prefix " + secret[:cut], secret[cut:] + b" suffix"]}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), b"PDF version one")
        partial = next(self.root.rglob("*.part")).read_bytes()
        self.assertTrue(partial.startswith(b"A" * 65536))
        self.assertNotIn(secret, partial)
        self.assertNotIn(SESSKEY.encode(), partial)
        self.assertFalse((self.root / "Week 1/notes_old.pdf").exists())

    def test_double_encoded_secret_split_inside_percent_escape_is_rejected(self):
        encoded = "".join(f"%{ord(char):02X}" for char in COOKIE)
        double_encoded = "".join(f"%{ord(char):02X}" for char in encoded).encode()
        chunks = [double_encoded[index:index + 7] for index in range(0, len(double_encoded), 7)]
        self.client.session.records[file().url] = {"chunks": [b"A" * 65536, *chunks]}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        partial = next(self.root.rglob("*.part")).read_bytes()
        self.assertNotIn(COOKIE.encode(), partial)
        self.assertNotIn(double_encoded, partial)

    def test_download_chunk_helper_reads_only_already_loaded_memory(self):
        class LazyClient:
            _sesskey = None
            session = SimpleNamespace(cookies=[])
            @property
            def sesskey(self):
                raise AssertionError("A lazy sesskey must not be accessed")
        response = FakeResponse(file().url, {"chunks": [b"alpha", b"beta"]})
        self.assertEqual(b"".join(sync.safe_download_chunks(response, LazyClient())), b"alphabeta")
        self.assertFalse(response.closed)  # The caller owns response cleanup.

    def test_missing_local_file_is_downloaded_again(self):
        result = self.run_sync()
        # Test fixture removal simulates a user moving their file; production never removes it.
        self.target(result).rename(self.root / "user-moved.pdf")
        self.assertEqual(self.run_sync()["new"]["count"], 1)
        self.assertEqual((self.root / "user-moved.pdf").read_bytes(), b"PDF version one")

    def test_backups_avoid_tracked_paths_even_if_the_tracked_file_is_missing(self):
        tracked = file("notes_old.pdf", cmid=11)
        self.client.items.append(activity(11))
        self.client.by_cmid[11] = [tracked]
        self.client.session.records[tracked.url] = {"body": b"separate tracked file"}
        first = self.run_sync()
        (self.root / "Week 1/notes_old.pdf").rename(self.root / "user-moved.pdf")
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"version two"}
        result = self.run_sync()
        self.assertEqual(result["updated"]["count"], 1)
        self.assertEqual(result["new"]["count"], 1)
        self.assertEqual((self.root / "Week 1/notes_old.pdf").read_bytes(), b"separate tracked file")
        self.assertEqual(self.bin.items, [("notes_old (1).pdf", b"PDF version one")])

    def test_state_changed_during_sync_is_not_overwritten(self):
        self.run_sync()
        path = self.root / sync.STATE_NAME
        foreign = b"{foreign state during concurrent run}"
        original_head = self.client.session.head
        def altered_head(url, **kwargs):
            path.write_bytes(foreign)
            return original_head(url, **kwargs)
        with patch.object(self.client.session, "head", side_effect=altered_head):
            with self.assertRaises(sync.SyncError):
                self.run_sync()
        self.assertEqual(path.read_bytes(), foreign)

    def test_preview_reserves_backups_before_planning_new_files(self):
        self.run_sync()
        self.client.session.records[file().url] = {"etag": '"v2"', "body": b"version two"}
        self.client.items.append(activity(11))
        self.client.by_cmid[11] = [file("notes_old.pdf", cmid=11)]
        preview = self.run_sync(dry_run=True)
        actual = self.run_sync()
        self.assertEqual(preview["new"]["files"], ["Week 1/notes_old (1).pdf"])
        self.assertEqual(preview["new"], actual["new"])
        self.assertEqual(preview["updated"], actual["updated"])
        self.assertEqual(self.bin.items, [("notes_old.pdf", b"PDF version one")])
        self.assertTrue((self.root / "Week 1/notes_old (1).pdf").exists())

    def test_root_changed_to_link_during_sync_is_rejected(self):
        original_head = self.client.session.head
        real_check = sync._is_link
        is_link = {"root": False}
        def changed_head(url, **kwargs):
            is_link["root"] = True
            return original_head(url, **kwargs)
        with patch.object(sync, "_is_link", side_effect=lambda path:
                          (path == self.root and is_link["root"]) or real_check(path)), \
                patch.object(self.client.session, "head", side_effect=changed_head):
            with self.assertRaises(sync.SyncError):
                self.run_sync()
        self.assertFalse(self.root.exists())

    def test_other_course_destination_is_rejected(self):
        self.run_sync()
        before = self.snapshot()
        self.client.items[0].course_id = 8
        with self.assertRaises(sync.SyncError):
            self.run_sync(course="8")
        self.assertEqual(self.snapshot(), before)

    def test_compressed_get_etag_does_not_look_like_an_update(self):
        url = self.client.by_cmid[10][0].url
        self.client.session.records[url] = {"etag": '"abc"', "get_etag": '"abc-gzip"'}
        self.run_sync()
        second = self.run_sync()
        self.assertEqual((second["unchanged"]["count"], second["updated"]["count"]), (1, 0))
        self.assertEqual(self.state()["files"][next(iter(self.state()["files"]))]["etag"], '"abc"')
        # Records written before the fix still carry the suffix.
        state = self.state()
        for entry in state["files"].values():
            entry["etag"] = '"abc-gzip"'
        (self.root / sync.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)
        self.assertEqual(sync._plain_etag('W/"x-br"'), '"x"')
        self.assertIsNone(sync._plain_etag(None))

    def test_kind_layout_flattens_sections_and_folders(self):
        self.client.sync_layout = "kind"
        self.client.items = [
            activity(10, "Lecture Material", "Lecture Material and Tutorials", kind="folder"),
            activity(11, "Exercises: E01", "1. Introduction"),
            activity(12, "Slides: Lecture Organization", "0. Organization"),
            activity(13, "🎁 Exam (Example)", "General"),
            activity(14, "study1.txt", "Monday, Oct 5th"),
        ]
        self.client.by_cmid = {
            10: [file("ds-01.pdf", 10, folder="exercise sheets", kind="folder"),
                 file("ds_1_basics.pdf", 10, folder="slides", kind="folder")],
            11: [file("DB-E01.pdf", 11)], 12: [file("DB-L00-Overview.pdf", 12)],
            13: [file("DB_Exam__SoSe_2023.pdf", 13)], 14: [file("study1.txt", 14)]}
        result = self.run_sync()
        self.assertEqual(sorted(result["new"]["files"]), [
            "Exams/DB_Exam__SoSe_2023.pdf", "Exercises/DB-E01.pdf", "Exercises/ds-01.pdf",
            "Other/study1.txt", "Slides/DB-L00-Overview.pdf", "Slides/ds_1_basics.pdf"])

    def test_courses_share_a_directory_with_separate_state_files(self):
        self.client.sync_layout = "kind"
        first = sync.sync_course(self.client, "7", self.root, dry_run=False, state_name=".moodle-sync-7.json")
        self.assertEqual(first["new"]["files"], ["Slides/notes.pdf"])
        other = FakeClient([activity(20, "Notes", course_id=8)], {20: [file("notes.pdf", 20)]})
        other.sync_layout = "kind"
        second = sync.sync_course(other, "8", self.root, dry_run=False, state_name=".moodle-sync-8.json")
        # Same name from another course: never overwritten, gets its own name.
        self.assertEqual(second["new"]["files"], ["Slides/notes (1).pdf"])
        self.assertTrue((self.root / ".moodle-sync-7.json").exists())
        self.assertTrue((self.root / ".moodle-sync-8.json").exists())
        again = sync.sync_course(self.client, "7", self.root, dry_run=False, state_name=".moodle-sync-7.json")
        self.assertEqual((again["unchanged"]["count"], again["removed_online"]["count"]), (1, 0))
        for name in ("moodle-sync.json", ".other.json", "../.moodle-sync-7.json"):
            with self.subTest(name=name), self.assertRaises(sync.SyncError):
                sync.sync_course(self.client, "7", self.root, state_name=name)

    def test_material_kind_reads_most_specific_name_first(self):
        kind = sync.material_kind
        self.assertEqual(kind(["ds-01", "exercise sheets", "Lecture Material", "Lecture Material and Tutorials"]), "Exercises")
        self.assertEqual(kind(["Examples of pipelining"]), "Other")
        self.assertEqual(kind(["Examples of pipelining", "Lecture 3"]), "Slides")
        self.assertEqual(kind(["20261021 - 01 Session 1 Exercise"]), "Exercises")
        self.assertEqual(kind(["Sarasvathy (2001)", "Sarasvathy's paper on effectuation (for Session 2)"]), "Other")
        self.assertEqual(kind(["Short Test 1 Sol"]), "Exams")
        self.assertEqual(kind(["Übungsblatt 3"]), "Exercises")
        self.assertEqual(kind(["lec01 2627"]), "Slides")
        self.assertEqual(kind(["HN3_Student_Code_of_Conduct copy", "Code of Conduct"], default="Slides"), "Other")
        self.assertEqual(kind([""], default="Slides"), "Slides")


class ReliabilityTests(unittest.TestCase):
    # Reuse only fixture helpers; do not execute all regression tests twice.
    setUp = SyncTests.setUp
    tearDown = SyncTests.tearDown
    run_sync = SyncTests.run_sync
    state = SyncTests.state
    target = SyncTests.target
    snapshot = SyncTests.snapshot
    def test_state_save_failure_recovers_without_duplicate_download(self):
        with patch.object(sync, "_write_state", side_effect=OSError("synthetic disk error")):
            with self.assertRaises(sync.SyncError):
                self.run_sync()
        self.assertTrue((self.root / "Week 1/notes.pdf").exists())
        self.assertTrue((self.root / (sync.STATE_NAME + ".journal.json")).exists())
        self.client.session.calls.clear()
        result = self.run_sync()
        self.assertEqual((result["new"]["count"], result["unchanged"]["count"]), (0, 1))
        self.assertFalse(list(self.root.rglob("* (1)*")))
        self.assertFalse(list(self.root.rglob("*.journal.json")))
        self.assertEqual([call[0] for call in self.client.session.calls], ["HEAD"])

    def test_process_exit_at_each_rename_boundary_recovers_one_update(self):
        self.run_sync()
        real_move = sync._move_no_replace
        for boundary in ("before_backup", "after_backup", "after_install"):
            with self.subTest(boundary=boundary):
                body = ("new bytes " + boundary).encode()
                self.client.session.records[file().url] = {"etag": '"' + boundary + '"', "body": body}
                def crash_move(source, target):
                    backup = "_old" in target.name
                    install = target.name == "notes.pdf" and source.name.endswith(".part")
                    if boundary == "before_backup" and backup:
                        raise KeyboardInterrupt()
                    real_move(source, target)
                    if (boundary == "after_backup" and backup) or (boundary == "after_install" and install):
                        raise KeyboardInterrupt()
                with patch.object(sync, "_move_no_replace", side_effect=crash_move):
                    with self.assertRaises(KeyboardInterrupt):
                        self.run_sync()
                before = self.snapshot()
                with self.assertRaises(sync.SyncError):
                    self.run_sync(dry_run=True)
                self.assertEqual(self.snapshot(), before)
                result = self.run_sync()
                self.assertEqual(result["unchanged"]["count"], 1)
                self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), body)
                artifact = result["artifacts"][0]
                self.assertEqual(artifact["version"], hashlib.sha256(body).hexdigest())
                self.assertTrue(all((self.root / item["local_path"]).exists() for item in artifact["history"]))
                self.assertFalse(list(self.root.rglob("notes (1).pdf")))
                self.assertFalse(list(self.root.rglob("*.journal.json")))

    def test_exit_after_state_commit_cleans_journal_idempotently(self):
        real_write = sync._write_state
        def crash_after_state(*args, **kwargs):
            real_write(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(sync, "_write_state", side_effect=crash_after_state):
            with self.assertRaises(KeyboardInterrupt):
                self.run_sync()
        self.assertTrue((self.root / (sync.STATE_NAME + ".journal.json")).exists())
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)
        self.assertFalse(list(self.root.rglob("*.journal.json")))

    def test_other_course_in_shared_directory_recovers_pending_file_first(self):
        state_name = ".moodle-sync-7.json"
        sync.sync_course(self.client, "7", self.root, dry_run=False, state_name=state_name)
        self.client.session.records[file().url] = {"etag": '"changed"', "body": b"new course seven bytes"}
        real_move = sync._move_no_replace
        def crash_after_backup(source, target):
            real_move(source, target)
            if "_old" in target.name:
                raise KeyboardInterrupt()
        with patch.object(sync, "_move_no_replace", side_effect=crash_after_backup):
            with self.assertRaises(KeyboardInterrupt):
                sync.sync_course(self.client, "7", self.root, dry_run=False, state_name=state_name)
        self.assertFalse((self.root / "Week 1/notes.pdf").exists())
        other = FakeClient([activity(course_id=8)], {10: [file()]})
        other.session.records[file().url] = {"body": b"course eight bytes"}
        result = sync.sync_course(other, "8", self.root, dry_run=False, state_name=".moodle-sync-8.json")
        self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), b"new course seven bytes")
        self.assertEqual(result["new"]["files"], ["Week 1/notes (1).pdf"])
        self.assertEqual((self.root / "Week 1/notes (1).pdf").read_bytes(), b"course eight bytes")
        self.assertFalse(list(self.root.rglob("*.journal.json")))

    def test_local_mutation_is_a_conflict_and_is_not_exported_to_planner(self):
        result = self.run_sync()
        target = self.target(result)
        target.write_bytes(b"my edited content")
        self.client.session.calls.clear()
        result = self.run_sync()
        self.assertEqual(result["local_modified"]["files"], ["Week 1/notes.pdf"])
        self.assertEqual(result["unchanged"]["count"], 0)
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["errors"][0]["code"], "local_modified")
        self.assertFalse(result["errors"][0]["retryable"])
        self.assertEqual(target.read_bytes(), b"my edited content")
        # Only a HEAD to learn whether the online file changed too; never a download.
        self.assertEqual([call[0] for call in self.client.session.calls], ["HEAD"])

    def test_without_validators_get_detects_content_change(self):
        self.client.session.records[file().url] = {"etag": None, "last_modified": None, "body": b"one"}
        self.run_sync()
        self.client.session.records[file().url]["body"] = b"two"
        result = self.run_sync()
        self.assertEqual(result["updated"]["count"], 1)
        self.assertEqual((self.root / "Week 1/notes.pdf").read_bytes(), b"two")
        self.assertEqual(self.bin.items, [("notes_old.pdf", b"one")])
        self.assertEqual(result["artifacts"][0]["history"], [])
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)

    def test_v1_state_is_upgraded_only_on_real_sync(self):
        self.run_sync()
        state = self.state()
        state["version"] = 1
        for item in state["files"].values():
            for key in ("sha256", "size", "history"):
                item.pop(key)
        path = self.root / sync.STATE_NAME
        path.write_text(json.dumps(state), encoding="utf-8")
        original = path.read_bytes()
        self.run_sync(dry_run=True)
        self.assertEqual(path.read_bytes(), original)
        result = self.run_sync()
        self.assertEqual(self.state()["version"], 2)
        self.assertEqual(result["artifacts"][0]["size"], len(b"PDF version one"))

    def test_page_label_attachments_and_text_are_accounted_for(self):
        self.client.items = [activity(kind="page"), activity(11, kind="label"),
                             activity(12, kind="assign"), activity(13, available=False)]
        self.client.by_cmid[11] = []
        self.client.sync_text = lambda item: {"text": "# Notice\nRead chapter 1.",
                                              "filename": f"notice-{item.id}.md", "url": item.url}
        result = self.run_sync(dry_run=True)
        self.assertFalse(self.root.exists())
        self.assertEqual(result["new"]["count"], 3)
        self.assertEqual([item["status"] for item in result["coverage"]],
                         ["checked", "checked", "unsupported", "unavailable"])
        result = self.run_sync()
        self.assertEqual((self.root / "Other/notice-11.md").read_text(encoding="utf-8"), "# Notice\nRead chapter 1.")
        self.assertEqual(len(result["artifacts"]), 3)

    def test_snapshot_secrets_and_corrupt_journal_never_reach_disk(self):
        self.client.items = [activity(kind="page")]
        self.client.by_cmid[10] = []
        self.client.sync_text = lambda item: {"text": "sesskey=" + SESSKEY, "filename": "notice.md", "url": item.url}
        result = self.run_sync()
        self.assertEqual(result["failed"]["count"], 1)
        self.assertNotIn(SESSKEY, json.dumps(result))
        self.assertFalse(list(self.root.rglob("*.md")))
        journal = self.root / (sync.STATE_NAME + ".journal.json")
        journal.write_text('{"version": 1, "cookie": "' + COOKIE + '"}', encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(sync.SyncError) as error:
            self.run_sync()
        self.assertNotIn(COOKIE, str(error.exception))
        self.assertEqual(before, self.snapshot())

    def test_recovery_journal_rejects_session_url_and_path_aliases(self):
        with patch.object(sync, "_write_state", side_effect=OSError("synthetic disk error")):
            with self.assertRaises(sync.SyncError):
                self.run_sync()
        path = self.root / (sync.STATE_NAME + ".journal.json")
        original = path.read_text(encoding="utf-8")
        self.assertNotIn(COOKIE, original)
        self.assertNotIn(SESSKEY, original)
        for mutation in ("session", "alias"):
            transaction = json.loads(original)
            if mutation == "session":
                transaction["entry"]["url"] += "?sesskey=" + SESSKEY
            else:
                transaction["temporary"] = transaction["entry"]["local_path"]
            path.write_text(json.dumps(transaction), encoding="utf-8")
            before = self.snapshot()
            with self.assertRaises(sync.SyncError) as error:
                self.run_sync()
            self.assertNotIn(SESSKEY, str(error.exception))
            self.assertEqual(self.snapshot(), before)
        path.write_text(original, encoding="utf-8")
        self.assertEqual(self.run_sync()["unchanged"]["count"], 1)

    def test_file_requests_use_client_transport_hook(self):
        methods = []
        def transport(method, url, **kwargs):
            methods.append(method)
            return getattr(self.client.session, method.lower())(url, **kwargs)
        self.client.sync_request = transport
        self.run_sync()
        self.assertEqual(methods, ["HEAD", "GET"])

    def test_rejected_login_stops_remaining_files_and_activities(self):
        class NotLoggedIn(Exception):
            pass
        self.client.items.append(activity(11))
        self.client.by_cmid[10].append(file("second.pdf"))
        self.client.by_cmid[11] = [file("third.pdf", cmid=11)]
        calls = []
        def rejected(method, url, **kwargs):
            calls.append((method, url))
            raise NotLoggedIn("must never expose " + COOKIE)
        self.client.sync_request = rejected
        result = self.run_sync()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.client.files_calls, [10])
        self.assertEqual(result["failed"]["count"], 3)
        self.assertTrue(all(item["code"] == "auth_failed" for item in result["errors"]))
        self.assertEqual(result["coverage"][-1]["status"], "auth_failed")
        self.assertNotIn(COOKIE, json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Windows directory mutex integration")
    def test_independent_process_cannot_sync_the_same_directory(self):
        test_file = str(Path(__file__).resolve())
        script = """
import importlib.util, sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
spec = importlib.util.spec_from_file_location('child_tests', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
client = m.FakeClient()
head = client.session.head
def waiting_head(url, **kwargs):
    print('locked', flush=True)
    sys.stdin.readline()
    return head(url, **kwargs)
client.session.head = waiting_head
m.sync.sync_course(client, '7', Path(sys.argv[2]), dry_run=False)
"""
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", script, test_file, str(self.root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8")
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(sync.SyncError) as error:
                self.run_sync()
            self.assertEqual(error.exception.code, "directory_busy")
            self.assertFalse(self.root.exists())
            self.assertFalse(self.client.session.calls)
            output, errors = child.communicate("continue\n", timeout=15)
            self.assertEqual(child.returncode, 0, errors)
            self.assertEqual(self.run_sync()["unchanged"]["count"], 1)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class ActivityNameTests(unittest.TestCase):
    """Analysis (121121): files are named after the Moodle activity, not the upload."""
    setUp = SyncTests.setUp
    tearDown = SyncTests.tearDown
    state = SyncTests.state
    COURSE = 121121

    def analysis(self):
        self.client.items = [activity(20, "Sheet 2", "29. Oktober", course_id=self.COURSE),
                             activity(21, "bonus test 1 sol", "29. Oktober", course_id=self.COURSE),
                             activity(22, "Material", "29. Oktober", kind="folder", course_id=self.COURSE)]
        self.client.by_cmid = {20: [file("LA_Sheets (31).pdf", 20)], 21: [file("bonus test sol.pdf", 21)],
                               22: [file("chapter 1.pdf", 22, kind="folder")]}
        self.client.sync_layout = "kind"

    def run_sync(self, dry_run=False):
        return sync.sync_course(self.client, self.COURSE, self.root, dry_run=dry_run)

    def test_display_name_rule(self):
        resource = activity(20, "Sheet 2", course_id=self.COURSE)
        self.assertEqual(sync.display_filename(resource, "LA_Sheets (31).pdf"), "Sheet 2.pdf")
        self.assertEqual(sync.display_filename(activity(20, "Sheet 2.pdf", course_id=self.COURSE), "x.pdf"),
                         "Sheet 2.pdf")
        self.assertEqual(sync.display_filename(activity(20, "  ", course_id=self.COURSE), "x.pdf"), "x.pdf")
        self.assertEqual(sync.display_filename(activity(20, " - ", course_id=self.COURSE), "x.pdf"), "x.pdf")
        self.assertEqual(sync.display_filename(activity(20, "Sheet  3.PDF", course_id=self.COURSE), "x.pdf"),
                         "Sheet 3.PDF")
        self.assertEqual(sync.display_filename(activity(20, "Sheet 2", course_id=7), "x.pdf"), "x.pdf")
        self.assertEqual(sync.display_filename(activity(22, "Material", kind="folder", course_id=self.COURSE),
                                               "chapter 1.pdf"), "chapter 1.pdf")

    def test_new_files_use_activity_names_and_bonus_tests_are_exams(self):
        self.analysis()
        result = self.run_sync()
        self.assertEqual(sorted(result["new"]["files"]), [
            "Exams/bonus test 1 sol.pdf", "Exercises/Sheet 2.pdf", "Slides/chapter 1.pdf"])
        # The upload's name stays the identity, so a later rename by the teacher is an update.
        self.assertIn(json.dumps([self.COURSE, 20, "", "LA_Sheets (31).pdf"], separators=(",", ":")),
                      self.state()["files"])
        self.assertEqual(self.run_sync()["unchanged"]["count"], 3)

    def test_files_synced_under_upload_names_are_renamed_once(self):
        self.analysis()
        with patch.object(sync, "ACTIVITY_NAMED_COURSES", frozenset()):
            first = self.run_sync()
        self.assertIn("Exercises/LA_Sheets (31).pdf", first["new"]["files"])
        self.assertIn("Exams/bonus test sol.pdf", first["new"]["files"])
        preview = self.run_sync(dry_run=True)
        self.assertEqual(sorted(preview["renamed"]["files"]), [
            "Exams/bonus test sol.pdf → Exams/bonus test 1 sol.pdf",
            "Exercises/LA_Sheets (31).pdf → Exercises/Sheet 2.pdf"])
        self.assertTrue((self.root / "Exercises/LA_Sheets (31).pdf").exists())  # Preview moves nothing.
        result = self.run_sync()
        self.assertEqual(result["renamed"], preview["renamed"])
        self.assertEqual((result["new"]["count"], result["unchanged"]["count"]), (0, 3))
        self.assertFalse((self.root / "Exercises/LA_Sheets (31).pdf").exists())
        self.assertEqual((self.root / "Exercises/Sheet 2.pdf").read_bytes(), b"PDF version one")
        self.assertEqual({row["local_path"] for row in result["artifacts"]},
                         {"Exercises/Sheet 2.pdf", "Exams/bonus test 1 sol.pdf", "Slides/chapter 1.pdf"})
        again = self.run_sync()
        self.assertEqual((again["renamed"]["count"], again["unchanged"]["count"]), (0, 3))

    def test_rename_interrupted_before_state_save_is_adopted(self):
        self.analysis()
        with patch.object(sync, "ACTIVITY_NAMED_COURSES", frozenset()):
            self.run_sync()
        with patch.object(sync, "_write_state", side_effect=OSError("synthetic crash")):
            with self.assertRaises(Exception):
                self.run_sync()
        result = self.run_sync()
        self.assertEqual((result["new"]["count"], result["failed"]["count"]), (0, 0))
        self.assertFalse(list(self.root.rglob("* (1).pdf")))
        self.assertTrue((self.root / "Exercises/Sheet 2.pdf").exists())

    def test_user_changed_file_is_not_renamed(self):
        self.analysis()
        with patch.object(sync, "ACTIVITY_NAMED_COURSES", frozenset()):
            self.run_sync()
        (self.root / "Exercises/LA_Sheets (31).pdf").write_bytes(b"my annotations")
        result = self.run_sync()
        self.assertNotIn("Exercises/LA_Sheets (31).pdf → Exercises/Sheet 2.pdf", result["renamed"]["files"])
        self.assertEqual((self.root / "Exercises/LA_Sheets (31).pdf").read_bytes(), b"my annotations")
        self.assertEqual(result["local_modified"]["files"], ["Exercises/LA_Sheets (31).pdf"])


class ShortCookieTests(unittest.TestCase):
    def test_short_cookie_values_are_not_treated_as_secrets(self):
        # A cookie such as "1" must not make every file and message look secret.
        cookies = [SimpleNamespace(value="1"), SimpleNamespace(value="true"),
                   SimpleNamespace(value=COOKIE)]
        client = SimpleNamespace(_sesskey=SESSKEY, session=SimpleNamespace(cookies=cookies))
        self.assertEqual(sync._secrets(client), {COOKIE, SESSKEY})
        chunks = list(sync.safe_download_chunks(
            FakeResponse(f"{BASE}/x.pdf", {"body": b"%PDF-1 true 1"}), client))
        self.assertEqual(b"".join(chunks), b"%PDF-1 true 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
