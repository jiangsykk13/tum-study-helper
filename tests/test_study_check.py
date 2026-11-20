"""Offline alert persistence, source isolation, thresholds and privacy."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import study_check as check


def item(**kw):
    return dict(title='虚构练习', course_key='INHN0002', kind='assignment',
                url='https://artemis.tum.de/courses/1/exercises/2',
                due_at='2026-11-12T23:59:00+01:00', **kw)


class CheckTests(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch('tempfile._os.mkdir', side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='study-check-')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('offline')))

    def apply(self, items=None, now='2026-11-01T09:00:00+01:00', **kw):
        report = dict(source='artemis:1', ok=True, items=items if items is not None else [item()])
        report.update(kw)
        return check.update(self.root, [report], now=now)

    def ack(self, result):
        check.acknowledge(self.root, [a['id'] for a in result['alerts']])

    def test_outbox_survives_restart_and_ack_is_idempotent(self):
        first = self.apply()
        self.assertEqual(len(first['alerts']), 1)
        self.assertEqual(first['alerts'], self.apply()['alerts'])
        self.ack(first)
        self.ack(first)
        self.assertEqual(self.apply()['alerts'], [])

    def test_thresholds_emit_once_and_only_most_urgent_on_first_late_observation(self):
        self.ack(self.apply())
        for now, expected in [('2026-11-06T09:00:00+01:00', 'due_168h'),
                              ('2026-11-11T09:00:00+01:00', 'due_48h'),
                              ('2026-11-12T09:00:00+01:00', 'due_24h')]:
            result = self.apply(now=now)
            self.assertEqual([a['type'] for a in result['alerts']], [expected])
            self.ack(result)
            self.assertEqual(self.apply(now=now)['alerts'], [])

    def test_failed_scope_retains_items_and_last_success(self):
        first = self.apply()
        self.ack(first)
        result = self.apply(ok=False, error='暂时无法登录。', now='2026-11-02T09:00:00+01:00')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['sources'][0]['last_success'], '2026-11-01T08:00:00+00:00')
        self.ack(result)
        self.assertEqual(self.apply(ok=False, error='暂时无法登录。')['alerts'], [])

    def test_failure_after_recovery_is_a_new_episode(self):
        self.ack(self.apply())
        self.ack(self.apply(ok=False, error='暂时无法登录。', now='2026-11-02T09:00:00+01:00'))
        self.ack(self.apply(now='2026-11-03T09:00:00+01:00'))
        result = self.apply(ok=False, error='暂时无法登录。', now='2026-11-04T09:00:00+01:00')
        self.assertEqual([a['type'] for a in result['alerts']], ['source_failed'])

    def test_date_change_creates_new_alert_and_new_threshold_cycle(self):
        self.ack(self.apply())
        changed = item()
        changed['due_at'] = '2026-11-03T23:00:00+01:00'
        result = self.apply([changed], now='2026-11-03T09:00:00+01:00')
        self.assertEqual([a['type'] for a in result['alerts']], ['deadline_changed'])
        self.ack(result)
        self.assertEqual(self.apply([changed], now='2026-11-03T10:00:00+01:00')['alerts'], [])

    def test_completed_does_not_emit_deadline_alert(self):
        self.ack(self.apply([item(completed=True)]))
        self.assertEqual(self.apply([item(completed=True)], now='2026-11-12T09:00:00+01:00')['alerts'], [])

    def test_same_activity_merges_scopes_and_keeps_conflict(self):
        self.apply()
        other = item()
        other['due_at'] = '2026-11-13T23:59:00+01:00'
        result = check.update(self.root, [{'source': 'moodle:calendar', 'ok': True, 'items': [other]}])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(len(result['items'][0]['conflict']), 2)

    def test_registration_and_exam_on_same_page_are_separate(self):
        registration, exam = item(), item()
        registration.update(kind='registration', completed=True)
        exam.update(kind='exam', due_at=None, raw_due='下周场次待定')
        result = self.apply([registration, exam])
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(len(result['alerts']), 1)

    def test_tumonline_list_rows_need_distinct_stable_keys(self):
        first = item()
        first.update(url='https://campus.tum.de/tumonline/', kind='grade', grade='1,7')
        with self.assertRaises(ValueError):
            check.normalize(first, 'tumonline:grades')
        first['merge_key'] = 'exam-a'
        second = dict(first, merge_key='exam-b', title='另一项成绩')
        result = check.update(self.root, [{'source':'tumonline:grades','ok':True,'items':[first,second]}])
        self.assertEqual(len(result['items']), 2)

    def test_credentials_external_urls_and_naive_dates_rejected_without_write(self):
        for field, value in [('url', 'https://campus.tum.de/tumonline/?access_token=private'),
                             ('url', 'https://other.example/'), ('details', 'password=private'),
                             ('due_at', '2026-11-10T12:00:00')]:
            broken = item()
            broken[field] = value
            with self.assertRaises((ValueError, check.planner.PlanError)):
                self.apply([broken])
            self.assertFalse((self.root / check.STATE).exists())

    def test_corrupt_state_not_overwritten(self):
        path = self.root / check.STATE
        path.write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(path.read_text(), '{bad')

    def test_grades_changed_only_when_visible_grade_changes(self):
        row = item()
        row.update(kind='grade', grade=None)
        self.assertEqual(self.apply([row])['alerts'], [])
        row['grade'] = '1,7'
        result = self.apply([row])
        self.assertEqual(result['alerts'][0]['type'], 'grade_changed')
        self.ack(result)
        self.assertEqual(self.apply([row])['alerts'], [])

    def test_resolved_and_expired_pending_alerts_are_not_replayed(self):
        self.apply()
        result = self.apply([item(completed=True)])
        self.assertEqual(result['alerts'], [])
        result = self.apply(now='2026-11-13T09:00:00+01:00')
        self.assertEqual(result['alerts'], [])

    def test_initial_expired_items_are_baseline_only(self):
        result = self.apply(now='2026-11-14T09:00:00+01:00')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['alerts'], [])

    def test_unknown_time_is_preserved_without_inventing_a_deadline(self):
        row = item()
        row.update(due_at=None, raw_due='11月的某一天，待确认')
        self.ack(self.apply([row]))
        self.assertEqual(self.apply([row], now='2026-11-12T09:00:00+01:00')['alerts'], [])
        self.assertIsNone(check.view(self.root)['items'][0]['due_at'])

    def test_invalid_batch_is_atomic(self):
        self.apply()
        before = (self.root / check.STATE).read_bytes()
        with self.assertRaises(ValueError):
            check.update(self.root, [{'source': 'moodle:valid', 'ok': True, 'items': []},
                                    {'source': 'external:invalid', 'ok': True, 'items': []}])
        self.assertEqual(before, (self.root / check.STATE).read_bytes())

    def test_collector_uses_guarded_functions_ingests_unchanged_and_redacts_pages(self):
        from types import SimpleNamespace
        entry = SimpleNamespace(completed=False, folder='INHN0002 Pretend', moodle_id=1,
                                artemis_id=None, moodle_dest=str(self.root/'Official'), artemis_dest=None)
        report = {'course_id': 1, 'new': {'count': 0}, 'updated': {'count': 0},
                  'unchanged': {'count': 1}, 'failed': {'count': 0}, 'artifacts': [{'safe': True}]}
        functions = {
            'list_courses': lambda **kw: [{'id': 1, 'name': 'Pretend'}],
            'upcoming_deadlines': lambda **kw: [],
            'sync_course': lambda **kw: report,
            'assignments': lambda **kw: [],
            'list_activities': lambda **kw: [],
            'read_page': lambda **kw: {'title': 'Pretend', 'text': 'Password: not-a-real-password', 'links': []}}
        owner = SimpleNamespace(tool_functions=lambda: functions, _secrets=lambda: set())
        with patch('course_map.load_course_map', return_value=[entry]), patch('study_materials.ingest', return_value={'ok': True}) as ingest:
            result = check.collect(self.root, sync=True, owner=owner)
        self.assertTrue(result['ok'])
        self.assertEqual(ingest.call_args.args[1], report)
        page = json.loads((Path(result['run_dir'])/'moodle-1-page.json').read_text(encoding='utf-8'))
        self.assertEqual(page['text'], 'Password: ***')
        functions['assignments'] = lambda **kw: {'error': 'failed'}
        with patch('course_map.load_course_map', return_value=[entry]):
            result = check.collect(self.root, owner=owner)
        by_source = {s['source']: s for s in result['sources']}
        self.assertFalse(by_source['moodle:1']['ok'])
        self.assertTrue(by_source['moodle:pages-1']['ok'])


    def forum_owner(self, posts):
        from types import SimpleNamespace
        entry = SimpleNamespace(completed=False, folder='INHN0002 Pretend', moodle_id=None,
                                artemis_id=1, moodle_dest=None, artemis_dest=str(self.root/'Official'))
        functions = {'list_courses': lambda **kw: [], 'upcoming_deadlines': lambda **kw: [],
                     'artemis_exercises': lambda **kw: [],
                     'artemis_channels': lambda **kw: [{'id': 20, 'can_read': True}, {'id': 21, 'can_read': False}],
                     'artemis_posts': posts}
        owner = SimpleNamespace(tool_functions=lambda: functions, _secrets=lambda: set())
        return entry, owner, functions

    def test_forum_collector_pages_snapshots_redacts_and_skips_unjoined_channels(self):
        calls = []
        def posts(**kw):
            calls.append(kw)
            page = kw['page']
            return {'posts': [{'id': page+1, 'content': 'Password: synthetic'}],
                    'has_more': page == 0, 'next_page': page+1 if page == 0 else None}
        entry, owner, _ = self.forum_owner(posts)
        with patch('course_map.load_course_map', return_value=[entry]):
            result = check.collect(self.root, owner=owner)
        self.assertTrue(result['ok'])
        self.assertEqual([c['page'] for c in calls], [0, 1])
        self.assertTrue(all(c['channel'] == '20' for c in calls))
        snapshot = result['pages'][0]
        self.assertEqual(snapshot['skipped_channels'], [21])
        self.assertEqual(len(snapshot['paths']), 3)
        self.assertEqual(snapshot['truncated_channels'], [])
        page = json.loads(Path(snapshot['paths'][1]).read_text(encoding='utf-8'))
        self.assertEqual(page['posts'][0]['content'], 'Password: ***')

    def test_forum_page_cap_exposes_continuation_and_channel_failure_does_not_hide_other_pages(self):
        def posts(**kw):
            if kw['channel'] == '22':
                return {'error': 'permission refused'}
            return {'posts': [{'id': kw['page']+1}], 'has_more': True, 'next_page': kw['page']+1}
        entry, owner, functions = self.forum_owner(posts)
        functions['artemis_channels'] = lambda **kw: [{'id': 20, 'can_read': True}, {'id': 22, 'can_read': True}]
        with patch('course_map.load_course_map', return_value=[entry]):
            result = check.collect(self.root, owner=owner)
        self.assertFalse(result['ok'])
        snapshot = result['pages'][0]
        self.assertEqual(snapshot['truncated_channels'], [{'channel_id': 20, 'next_page': 3}])
        self.assertEqual(snapshot['failed_channels'], [22])
        self.assertEqual(len(snapshot['paths']), 4)
        sources = {s['source']: s for s in result['sources']}
        self.assertTrue(sources['artemis:1']['ok'])
        self.assertFalse(sources['artemis:forum-pages-1']['ok'])

    def test_forum_failure_preserves_previous_items_and_repeated_page_is_failure(self):
        check.update(self.root, [{'source': 'artemis:forum-pages-1', 'ok': True, 'items': [item()]}])
        entry, owner, functions = self.forum_owner(lambda **kw: {'posts': [{'id': 1}], 'has_more': True, 'next_page': 1})
        with patch('course_map.load_course_map', return_value=[entry]):
            result = check.collect(self.root, owner=owner)
        self.assertEqual(result['pages'][0]['failed_channels'], [20])
        self.assertEqual(len(check.view(self.root)['items']), 1)
        functions['artemis_channels'] = lambda **kw: {'error': 'failed'}
        with patch('course_map.load_course_map', return_value=[entry]):
            result = check.collect(self.root, owner=owner)
        self.assertFalse(result['ok'])
        self.assertIn('error', result['pages'][0])
        self.assertEqual(len(check.view(self.root)['items']), 1)


if __name__ == '__main__':
    unittest.main()
