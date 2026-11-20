"""Read-only run state, inherited resources and unambiguous plan totals."""
from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import planner

presentation = planner.sibling('planner_presentation')
NOW = datetime.fromisoformat('2026-11-06T12:00:00+01:00')
DAY = '2026-11-06'


class PresentationTest(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch('tempfile._os.mkdir', side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='plan-view-test-')))
        shutil.copytree(ROOT / 'tests/fixtures/planner', self.root, dirs_exist_ok=True)
        self.data = {'date': '2026-11-05', 'today': DAY, 'dates': ['2026-11-05'], 'status': None}

    def write(self, path, data, stamp='2026-11-06T11:50:00+01:00'):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')
        seconds = datetime.fromisoformat(stamp).timestamp()
        os.utime(path, (seconds, seconds))
        return path

    def run_receipt(self, **changes):
        data = {'version': 1, 'target_date': DAY, 'run_id': 'run-1', 'published': False,
                'updated_at': '2026-11-06T11:45:00+01:00',
                'stages': {'sync': {'ok': True, 'at': '2026-11-06T11:40:00+01:00'}}}
        data.update(changes)
        return self.write(f'运行/{DAY}.json', data)

    def test_draft_missing_today_and_recent_running_are_distinct(self):
        self.run_receipt()
        self.write(f'草稿/{DAY}.json', {'version': 2, 'tasks': []})
        report = presentation.generation(self.root, self.data, planner, now=NOW)
        self.assertEqual(report['state'], 'running')
        self.assertTrue(report['draft_available'])
        self.assertTrue(report['today_missing'])
        self.assertEqual((report['shown_date'], report['target_date']), ('2026-11-05', DAY))
        self.assertEqual(report['last_stage'], 'sync')

    def test_stale_uses_latest_draft_activity_and_strict_sixty_minutes(self):
        old = '2026-11-06T09:00:00+01:00'
        path = self.run_receipt(updated_at=old, stages={})
        seconds = datetime.fromisoformat(old).timestamp()
        os.utime(path, (seconds, seconds))
        self.write(f'草稿/{DAY}.json', {}, stamp='2026-11-06T11:00:00+01:00')
        self.assertEqual(presentation.generation(self.root, self.data, planner, now=NOW)['state'], 'running')
        later = datetime.fromisoformat('2026-11-06T12:00:01+01:00')
        self.assertEqual(presentation.generation(self.root, self.data, planner, now=later)['state'], 'stale')

    def test_matching_formal_is_authoritative_over_failed_or_missing_receipt(self):
        self.run_receipt()
        self.data['status'] = {'plan_date': DAY, 'ok': False, 'message': 'receipt failed', 'run_id': 'run-1'}
        self.write(f'计划/{DAY}.json', {'version': 2, 'date': DAY, 'run_id': 'run-1', 'tasks': []})
        before = {str(p): p.read_bytes() for p in self.root.rglob('*.json')}
        self.assertEqual(presentation.generation(self.root, self.data, planner, now=NOW)['state'], 'published')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*.json')})
        self.write(f'计划/{DAY}.json', {'version': 2, 'date': DAY, 'run_id': 'other-run', 'tasks': []})
        self.assertEqual(presentation.generation(self.root, self.data, planner, now=NOW)['state'], 'failed')

    def test_failure_is_redacted_and_corrupt_receipt_does_not_hide_old_plan(self):
        self.data['status'] = {'ok': False, 'plan_date': DAY, 'message': 'jwt=private-value'}
        report = presentation.generation(self.root, self.data, planner, now=NOW)
        self.assertEqual(report['state'], 'failed')
        self.assertNotIn('private-value', report['message'])
        path = self.write(f'运行/{DAY}.json', {})
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual(presentation.generation(self.root, self.data, planner, now=NOW)['state'], 'failed')

    def test_legacy_view_adds_fields_and_never_writes(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = planner.view(self.root, today=date(2026, 11, 6))
        self.assertIn('generation', result)
        self.assertIn('summary', result)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_resources_recovered_for_current_and_previous_without_rewriting(self):
        original = planner.load_plan(self.root, '2026-10-22')
        original['tasks'][0].update(materials=[{'path': 'I:/fiction/notes.pdf', 'pages': [2, 4]}],
                                    practice={'folder': 'I:/fiction/problem'}, due_at='2026-11-05T23:59:00+01:00')
        self.write('计划/2026-10-22.json', original)
        copy = {'id': 'old-copy', 'carry_of': '2026-10-22/' + original['tasks'][0]['id']}
        data = {'dates': ['2026-10-22'], 'plan': {'tasks': [copy]}, 'previous': {'tasks': [dict(copy, id='older')]}}
        presentation.inherit_resources(self.root, data, planner)
        for task in [data['plan']['tasks'][0], data['previous']['tasks'][0]]:
            self.assertEqual(task['practice']['folder'], 'I:/fiction/problem')
            self.assertEqual(task['materials'][0]['pages'], [2, 4])
            self.assertEqual(task['due_at'], '2026-11-05T23:59:00+01:00')
        self.assertNotIn('materials', planner.load_plan(self.root, '2026-10-23')['tasks'][0])
        missing = {'id': 'missing', 'carry_of': '2020-01-01/unknown'}
        presentation.inherit_resources(self.root, {'dates': [], 'plan': {'tasks': [missing]}}, planner)
        self.assertIn('找不到原任务', missing['resource_note'])

    def test_catchup_cannot_inflate_today_progress_and_ranges_are_deduplicated(self):
        tasks = [{'id': 'fresh', 'minutes': 60, 'section': 'course'},
                 {'id': 'research', 'minutes': 40, 'section': 'research'},
                 *[{'id': f'copy{i}', 'minutes': 90, 'auto_carry': True, 'carry_of': 'old/item',
                    'work_span': {'start': 0, 'end': 5400}} for i in range(2)]]
        result = {'plan': {'tasks': tasks, 'targets': {'course_hours': '6–8'}},
                  'states': {'fresh': 'half', 'research': 'done', 'copy0': 'done', 'copy1': 'done'},
                  'derived': {'debt': {'total_hours': 12}}}
        totals = presentation.summary(result, planner)
        self.assertEqual((totals['planned_minutes'], totals['completed_minutes']), (100, 70))
        self.assertEqual(totals['carried_completed_minutes'], 90)
        self.assertEqual((totals['target_min_minutes'], totals['target_max_minutes']), (360, 480))
        self.assertEqual(presentation.target_minutes('6-8 小时'), (360, 480))
        self.assertEqual(presentation.target_minutes('bad'), (None, None))

    def test_due_at_is_optional_but_must_be_an_aware_timestamp(self):
        plan = planner.load_plan(self.root, '2026-10-23')
        plan['tasks'][0]['due_at'] = '2026-11-06T23:59:00'
        self.assertTrue(any('due_at' in p for p in planner.check_plan(self.root, '2026-10-23', data=plan)))
        plan['tasks'][0]['due_at'] = '2026-11-06T23:59:00+01:00'
        self.assertEqual(planner.check_plan(self.root, '2026-10-23', data=plan), [])


if __name__ == '__main__':
    unittest.main()
