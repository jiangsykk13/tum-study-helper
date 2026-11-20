"""Synthetic Entrepreneurship layout, shared versions and migration regressions."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sync as fixtures
FakeClient, activity, file, sync = fixtures.FakeClient, fixtures.activity, fixtures.file, fixtures.sync
import material_reorganize as reorganizer
import study_materials as materials
import entrepreneurship as policy


class ProfileTests(unittest.TestCase):
    setUp = fixtures.SyncTests.setUp
    tearDown = fixtures.SyncTests.tearDown

    def readings(self, root=None):
        ids = (3971840, 3971843)
        self.client = FakeClient([activity(n, "Sarasvathy paper", "Session 2", course_id=122369) for n in ids],
                                 {n: [file("Sarasvathy.pdf", n)] for n in ids})
        self.client.sync_layout = "kind"
        return self.client

    def test_layout_uses_source_session_and_reading_override(self):
        classify = sync.material_kind
        for course, cmid, name, expected in (
                (122369, 3971847, "Session 2.pdf", ("Slides", "Lecture S02 - Session 2.pdf")),
                (122367, 3961930, "Session 2.pdf", ("Exercises", "Exercise S02 - Session 2.pdf")),
                (122367, 3961935, "Assess your knowledge.pdf", ("Exercises", "Exercise S02 - Assess your knowledge.pdf")),
                (122367, 3961933, "Aldrich.pdf", ("Other", "Reading - Aldrich.pdf")),
                (122369, 9, "Exam.pdf", ("Exams", "Lecture S02 - Exam.pdf"))):
            self.assertEqual(policy.layout(activity(cmid, name="Session 2", section="Session 2 | date", course_id=course),
                                           name, [], classify), expected)
        self.assertIsNone(policy.layout(activity(course_id=7), "Session 2.pdf", [], classify))
        self.assertEqual(policy.layout(activity(name="General", course_id=122367, section="General"), "a.pdf", [], classify),
                         ("Exercises", "Exercise - a.pdf"))

    def test_shared_paper_repeat_sync_and_source_divergence(self):
        for changed in (3971840, 3971843):
            with self.subTest(changed=changed):
                client = self.readings()
                root = self.root / str(changed)
                result = sync.sync_course(client, 122369, root, False)
                self.assertEqual(len(list(root.rglob("*.pdf"))), 1)
                self.assertEqual(len(result["artifacts"]), 2)
                state, _ = sync._read_state(root, set())
                rows = list(state["files"].values())
                self.assertEqual(state["version"], 3)
                self.assertNotIn("alias_of", rows[0])
                self.assertIn("alias_of", rows[1])
                repeat = sync.sync_course(client, 122369, root, False)
                self.assertEqual(repeat["unchanged"]["count"], 2)
                client.session.records[client.by_cmid[changed][0].url] = {"body": b"changed", "etag": '"v2"'}
                result = sync.sync_course(client, 122369, root, False)
                self.assertEqual(result["failed"]["count"], 0, result)
                state, _ = sync._read_state(root, set())
                self.assertEqual(len({r["local_path"] for r in state["files"].values()}), 2)
                current = next(r for r in state["files"].values() if r["cmid"] == changed)
                self.assertEqual((root / current["local_path"]).read_bytes(), b"changed")
                # The shared old version is still the other source's current file: kept, not trashed.
                self.assertEqual(current["history"], [])
                other = next(r for r in state["files"].values() if r["cmid"] != changed)
                self.assertTrue((root / other["local_path"]).is_file())
                self.assertTrue(all("alias_of" not in row for row in state["files"].values()))
                self.assertEqual(sync.sync_course(client, 122369, root, False)["unchanged"]["count"], 2)

    def test_split_crash_recovers_and_invalid_alias_is_rejected(self):
        client = self.readings()
        sync.sync_course(client, 122369, self.root, False)
        client.session.records[client.by_cmid[3971840][0].url] = {"body": b"new version", "etag": '"v2"'}
        with patch.object(sync, "_write_state", side_effect=SystemExit("synthetic crash")):
            with self.assertRaises(SystemExit):
                sync.sync_course(client, 122369, self.root, False)
        result = sync.sync_course(client, 122369, self.root, False)
        self.assertEqual(result["failed"]["count"], 0)
        self.assertFalse(list(self.root.glob("*.journal.json")))
        state, _ = sync._read_state(self.root, set())
        next(iter(state["files"].values()))["alias_of"] = "missing"
        (self.root / sync.STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            sync._read_state(self.root, set())

    def test_shared_local_edits_are_not_overwritten(self):
        client = self.readings()
        first = sync.sync_course(client, 122369, self.root, False)
        target = self.root / first["artifacts"][0]["local_path"]
        target.write_bytes(b"local edits")
        result = sync.sync_course(client, 122369, self.root, False)
        self.assertEqual(result["local_modified"]["count"], 2)
        self.assertEqual(target.read_bytes(), b"local edits")

    def test_missing_shared_file_can_restore_while_one_source_changes(self):
        client = self.readings()
        first = sync.sync_course(client, 122369, self.root, False)
        (self.root / first["artifacts"][0]["local_path"]).unlink()
        client.session.records[client.by_cmid[3971840][0].url] = {"body": b"changed", "etag": '"v2"'}
        result = sync.sync_course(client, 122369, self.root, False)
        self.assertEqual(result["failed"]["count"], 0, result)
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertEqual(len(list(self.root.rglob("*.pdf"))), 2)
        sync._read_state(self.root, set())


class MigrationTests(unittest.TestCase):
    setUp = fixtures.SyncTests.setUp
    tearDown = fixtures.SyncTests.tearDown

    def fixture(self):
        self.planner = self.root / "Planner"
        self.official = self.root / "Semester Test" / "MGTHN0133 Entrepreneurship" / "Official"
        self.official.mkdir(parents=True)
        self.planner.mkdir()
        self.map = self.root / "map.md"
        self.map.write_text("## Semester (`Semester Test`)\n"
                            "| 文件夹 | 课程 | Moodle 编号 |\n|---|---|---|\n"
                            "| `MGTHN0133 Entrepreneurship` | Lecture | 122369 |\n"
                            "| | Exercise | 122367 |\n", encoding="utf-8")
        definitions = [(122369, 3971847, "Session 2.pdf", "Slides/Session 2.pdf", b"lecture"),
                       (122369, 3971840, "Sarasvathy.pdf", "Other/Sarasvathy.pdf", b"paper"),
                       (122369, 3971843, "Sarasvathy.pdf", "Other/Sarasvathy (1).pdf", b"paper"),
                       (122367, 3961930, "Session 2.pdf", "Other/Session 2.pdf", b"exercise")]
        for course in policy.COURSES:
            state = {"version": 2, "course_id": course, "files": {}}
            activities = []
            for c, cmid, name, relative, body in definitions:
                if c != course:
                    continue
                target = self.official / relative
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(body)
                identity = sync._identity(c, cmid, "", name)
                state["files"][identity] = {"course_id": c, "cmid": cmid, "filename": name,
                    "folder_path": "", "local_path": relative, "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body), "history": [], "url": file(name, cmid).url,
                    "etag": '"v1"', "last_modified": None, "last_sync": "2030-01-01T00:00:00+00:00"}
                activities.append({"id": cmid, "name": "Session 2", "section": "Session 2", "course_id": c})
            (self.official / f".moodle-sync-{course}.json").write_text(json.dumps(state), encoding="utf-8")
            run = self.planner / "检查" / "2030-01-01"
            run.mkdir(parents=True, exist_ok=True)
            (run / f"moodle-{course}-activities.json").write_text(json.dumps(activities), encoding="utf-8")
            report = {"dry_run": False, "course_id": course, "artifacts": sync._artifacts(self.official, state, set())}
            materials.ingest(self.planner, report, self.official, "moodle", "MGTHN0133")
        self.before_index = materials._read(self.planner)

    def run_migration(self, apply):
        return reorganizer.reorganize(self.planner, apply=apply, map_path=self.map)

    def test_preview_is_zero_write_apply_is_idempotent_and_ids_survive(self):
        self.fixture()
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        preview = self.run_migration(False)
        self.assertEqual(preview["files"], 3)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        paper_ids = [key for key, item in self.before_index["items"].items() if item["filename"].startswith("Sarasvathy")]
        materials.record_summary(self.planner, paper_ids[0], "pages one", pages=[1, 2], page_count=4)
        materials.record_summary(self.planner, paper_ids[1], "pages two", pages=[3, 4], page_count=4)
        result = self.run_migration(True)
        self.assertEqual(result["files"], 3)
        self.assertEqual(len(list(self.official.rglob("*.pdf"))), 3)
        after = materials._read(self.planner)
        self.assertEqual(set(after["items"]), set(self.before_index["items"]))
        for key, item in after["items"].items():
            self.assertEqual(materials.resolve(self.planner, key, item["version"])["version"], item["version"])
        self.assertEqual(materials.resolve(self.planner, paper_ids[0])["read_ranges"], [[1, 4]])
        materials.record_summary(self.planner, paper_ids[0], "all pages", complete=True)
        self.assertEqual(materials.pending(self.planner)["total"], 3)
        materials.record_summary(self.planner, paper_ids[0], "", skip_reason="fictional skip")
        self.assertEqual(materials.pending(self.planner)["total"], 2)
        self.assertFalse(self.run_migration(True)["changed"])
        self.assertEqual(len(list((self.planner / "备份").iterdir())), 1)
        self.assertTrue((Path(result["backup"]) / "Official" / "Other" / "Sarasvathy (1).pdf").exists())

    def test_interrupted_metadata_commit_recovers_before_sync(self):
        self.fixture()
        atomic = reorganizer._atomic
        def crash(path, payload):
            if path == self.official / ".moodle-sync-122367.json":
                raise SystemExit("synthetic interruption")
            return atomic(path, payload)
        with patch.object(reorganizer, "_atomic", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.run_migration(True)
        with self.assertRaises(sync.SyncError):
            sync._recover_directory(self.official, set(), True)
        sync._recover_directory(self.official, set(), False)
        self.assertFalse((self.official / reorganizer.MARKER).exists())
        self.assertEqual(len(list(self.official.rglob("*.pdf"))), 3)
        for key, item in self.before_index["items"].items():
            materials.resolve(self.planner, key, item["version"])

    def test_modified_input_and_foreign_target_are_preserved(self):
        self.fixture()
        foreign = self.official / "Exercises" / "Exercise S02 - Session 2.pdf"
        foreign.parent.mkdir()
        foreign.write_bytes(b"user file")
        result = self.run_migration(True)
        self.assertEqual(foreign.read_bytes(), b"user file")
        self.assertTrue(any(" (1)" in r["new"] for r in result["operations"]))
        managed = self.official / "Slides" / "Lecture S02 - Session 2.pdf"
        managed.write_bytes(b"local edits")
        with self.assertRaises(materials.MaterialError):
            self.run_migration(True)
        self.assertEqual(managed.read_bytes(), b"local edits")

    def test_published_plan_resolves_moved_file_without_rewriting_json(self):
        self.fixture()
        import planner
        item = next(row for row in self.before_index["items"].values() if row["course_id"] == 122367)
        plan = {"version": 1, "date": "2030-01-01", "title": "Fictional plan", "tasks": [
            {"id": "en-test", "title": "Read", "section": "courses", "course_key": "MGTHN0133",
             "minutes": 30, "materials": [{"id": item["id"], "version": item["version"], "path": item["path"]}]}]}
        folder = self.planner / "计划"
        folder.mkdir()
        saved = folder / "2030-01-01.json"
        saved.write_text(json.dumps(plan), encoding="utf-8")
        before = saved.read_bytes()
        self.run_migration(True)
        result = planner.view(self.planner, "2030-01-01")
        ref = result["plan"]["tasks"][0]["materials"][0]
        self.assertTrue(Path(ref["path"]).is_file())
        self.assertIn("Exercise S02", ref["path"])
        self.assertEqual(saved.read_bytes(), before)

    def test_recovery_refuses_external_metadata_change(self):
        self.fixture()
        atomic = reorganizer._atomic
        def crash(path, payload):
            if path == self.official / ".moodle-sync-122369.json":
                raise SystemExit("synthetic interruption")
            return atomic(path, payload)
        with patch.object(reorganizer, "_atomic", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.run_migration(True)
        index_path = self.planner / materials.INDEX
        index_path.write_bytes(index_path.read_bytes() + b"\n")
        with self.assertRaises(materials.MaterialError):
            sync._recover_directory(self.official, set(), False)
        self.assertTrue((self.official / reorganizer.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
