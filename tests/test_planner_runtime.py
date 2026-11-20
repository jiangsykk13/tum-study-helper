"""Recovery, fixed debt budgets and v2 publication: all files are fictional/temp."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('planner', ROOT / 'planner.py')
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)
runtime = planner.sibling('planner_runtime')


def instant(value='2026-10-23T08:00:00+00:00'):
    return datetime.fromisoformat(value)


def task(name='root', minutes=90, **kw):
    return {'id': name, 'title': name, 'section': 'course', 'course_key': 'INHN0001',
            'course': '信息学导论', 'kind': 'exercise', 'required': True, 'origin': 'new', 'minutes': minutes,
            'links': [{'text': '课程', 'url': 'https://www.moodle.tum.de/course/view.php?id=1'}], **kw}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        mkdir = os.mkdir
        with patch('tempfile._os.mkdir', side_effect=lambda path, mode=0o777, **kw: mkdir(path, 0o777, **kw)):
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='planner-runtime-')))
        settings = f'''## 1. 时间
| | 课程学习 | 科研与课外 |
|---|---|---|
| 周一至周五 | 6–8 小时，按 7 小时排 | 2 小时 |
| 周六 | 3 小时 | 3 小时 |
| 周日 | 3 小时 | 2 小时 |
## 2. 课程
本地文件夹（在 `{self.root / 'Courses'}` 下）
| 代号 | 名字 | 英文 | 比例 | Moodle | Artemis | 日历 | 文件夹 |
| INHN0001 | 信息学导论 | Informatics | 60%（和编程基础合计） | 1 | | Test | Informatics |
| INHN0002 | 编程基础 | Programming | （同上） | 2 | 1 | Test | Programming |
| INHN0004 | 离散结构 | Discrete | 40% | 3 | | Test | Discrete |
## 6. 特殊日期
| 日期 | 情况 | 怎么排 |
| 2026-10-25 | 休息日 | 不排任务 |
'''
        (self.root / '规划设定.md').write_text(settings, encoding='utf-8')

    def plan(self, day, tasks, **kw):
        value = {'version': 1, 'date': day, 'tasks': tasks, **kw}
        planner._write_json(self.root / '计划' / (day + '.json'), value)
        return value

    def draft(self, day='2026-10-24', tasks=None):
        prepared = runtime.prepare(self.root, target_date=day, now=instant())
        path = Path(prepared['draft_path'])
        data = json.loads(path.read_text(encoding='utf-8'))
        data['tasks'] = tasks if tasks is not None else [task()]
        planner._write_json(path, data)
        return path, data

    def left(self):
        items = planner.debt(self.root, '2026-10-26')['items']
        return items[0]['minutes_left'] if items else 0

    def test_float_version_cannot_publish_an_unreadable_formal_file(self):
        path, data = self.draft()
        data['version'] = 2.0
        planner._write_json(path, data)
        with self.assertRaises(planner.PlanError):
            runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertFalse((self.root / '计划' / '2026-10-24.json').exists())

    def test_prefix_rescheduling_and_duplicate_copies_never_erase_work(self):
        self.plan('2026-10-22', [task()])
        copy = task('copy', 50, origin='debt', carry_of='2026-10-22/root', work_span={'start': 0, 'end': 3000})
        self.plan('2026-10-23', [copy])
        self.plan('2026-10-24', [copy])
        self.assertEqual(self.left(), 90)
        planner.set_state(self.root, '2026-10-23', 'copy', 'half')
        self.assertEqual(self.left(), 65)
        planner.set_state(self.root, '2026-10-24', 'copy', 'half')
        self.assertEqual(self.left(), 65)
        planner.set_state(self.root, '2026-10-24', 'copy', 'done')
        self.assertEqual(self.left(), 40)
        planner.set_state(self.root, '2026-10-23', 'copy', 'done')
        planner.set_state(self.root, '2026-10-24', 'copy', None)
        self.assertEqual(self.left(), 40)
        planner.set_state(self.root, '2026-10-22', 'root', 'half')
        self.assertEqual(self.left(), 40)
        planner.set_state(self.root, '2026-10-22', 'root', 'done')
        self.assertEqual(self.left(), 0)
        planner.set_state(self.root, '2026-10-22', 'root', None)
        self.assertEqual(self.left(), 40)
        planner.set_state(self.root, '2026-10-24', 'copy', 'dropped')
        self.assertEqual(self.left(), 0)
        planner.set_state(self.root, '2026-10-24', 'copy', None)
        self.assertEqual(self.left(), 40)

    def publish_day(self, day, tasks):
        path, _ = self.draft(day, tasks)
        result = runtime.publish(self.root, day, path, now=instant())
        return result, planner.load_plan(self.root, day)

    def test_unfinished_work_is_carried_every_day_on_top_of_the_budget(self):
        self.plan('2026-10-22', [task(), task('finished', 30), task('extra', 30, required=False)])
        planner.set_state(self.root, '2026-10-22', 'finished', 'done')
        result, plan = self.publish_day('2026-10-24', [task('new', 170)])  # Saturday: 3 hours of courses
        self.assertEqual(result['carried'], 1)
        copy = plan['tasks'][-1]
        self.assertEqual((copy['id'], copy['carry_of'], copy['work_span'], copy['minutes'], copy['origin_note']),
                         ('c-root', '2026-10-22/root', {'start': 0, 'end': 5400}, 90, '顺延'))
        self.assertTrue(copy['auto_carry'])
        self.assertEqual(planner.check_plan(self.root, '2026-10-24'), [])  # 260 minutes, but only 170 count
        # Half done on the old day: the next plan carries the second half, again on top of its own tasks.
        planner.set_state(self.root, '2026-10-22', 'root', 'half')
        planner.set_state(self.root, '2026-10-24', 'new', 'done')
        result, plan = self.publish_day('2026-10-26', [task('monday', 60)])
        carried = [t for t in plan['tasks'] if t.get('auto_carry')]
        self.assertEqual([(t['id'], t['work_span'], t['minutes']) for t in carried],
                         [('c-root', {'start': 2700, 'end': 5400}, 45)])
        self.assertTrue(runtime.carry(self.root, '2026-10-26', now=instant())['unchanged'])
        planner.set_state(self.root, '2026-10-26', 'c-root', 'done')
        self.assertTrue(runtime.carry(self.root, '2026-10-26', now=instant())['unchanged'])
        self.assertEqual(planner.debt(self.root, '2026-10-26')['items'], [])  # all older work is done

    def test_scheduled_part_stays_and_only_the_rest_is_added(self):
        self.plan('2026-10-22', [task()])
        part = task('part', 30, origin='debt', carry_of='2026-10-22/root', work_span={'start': 0, 'end': 1800})
        result, plan = self.publish_day('2026-10-24', [part])
        self.assertEqual([(t['id'], t.get('auto_carry', False), t['work_span']) for t in plan['tasks']],
                         [('part', False, {'start': 0, 'end': 1800}), ('c-root', True, {'start': 1800, 'end': 5400})])

    def test_finished_items_lose_untouched_copies_and_are_not_listed_twice(self):
        self.plan('2026-10-23', [task(), task('urgent', 30, kind='deadline')])
        self.publish_day('2026-10-24', [task('new', 60)])
        plan = planner.load_plan(self.root, '2026-10-24')
        self.assertEqual([t['id'] for t in plan['tasks'] if t.get('auto_carry')], ['c-urgent', 'c-root'])
        self.assertIsNone(planner.view(self.root, '2026-10-24', today=instant().date())['previous'])
        planner.set_state(self.root, '2026-10-23', 'root', 'done')
        self.assertEqual(runtime.carry(self.root, '2026-10-24', now=instant())['carried'], 1)
        plan = planner.load_plan(self.root, '2026-10-24')
        self.assertEqual([t['id'] for t in plan['tasks'] if t.get('auto_carry')], ['c-urgent'])
        with self.assertRaises(planner.PlanError):
            runtime.carry(self.root, '2026-10-22', now=instant())  # past days stay as they were

    def test_allocate_only_remaining_contiguous_work_and_direct_root(self):
        self.plan('2026-10-22', [task()])
        self.plan('2026-10-23', [task('part', 50, origin='debt', carry_of='2026-10-22/root',
                                       work_span={'start': 0, 'end': 3000})])
        planner.set_state(self.root, '2026-10-23', 'part', 'half')
        span = planner.allocate(self.root, '2026-10-22/root', 25, target_date='2026-10-24')
        self.assertEqual(span['work_span'], {'start': 1500, 'end': 3000})
        with self.assertRaises(planner.PlanError):
            planner.allocate(self.root, '2026-10-23/part', 25, target_date='2026-10-24')
        runtime.prepare(self.root, target_date='2026-10-24', now=instant())
        planner._write_json(self.root / '草稿' / '2026-10-24.json', {'tasks': [task('next', 25, origin='debt',
                            carry_of=span['carry_of'], work_span=span['work_span'])]})
        following = planner.allocate(self.root, '2026-10-22/root', 30, target_date='2026-10-24')
        self.assertEqual(following['work_span'], {'start': 3000, 'end': 4800})

    def test_feedback_is_optional_preserved_clearable_and_does_not_complete_work(self):
        self.plan('2026-10-22', [task()])
        planner.set_state(self.root, '2026-10-22', 'root', 'half', actual_minutes=70, understanding='hard')
        planner.set_state(self.root, '2026-10-22', 'root', 'no')
        data = planner.view(self.root, '2026-10-22')
        self.assertEqual(data['feedback']['root']['actual_minutes'], 70)
        self.assertEqual(data['feedback']['root']['understanding'], 'hard')
        self.assertEqual(self.left(), 90)
        planner.set_state(self.root, '2026-10-22', 'root', None, actual_minutes=None, understanding=None)
        self.assertNotIn('root', planner.view(self.root, '2026-10-22')['feedback'])
        for value in (0, True, -1, 1441, '75', 1.5):
            with self.subTest(value=value), self.assertRaises(planner.PlanError):
                planner.set_state(self.root, '2026-10-22', 'root', None, actual_minutes=value)

    def test_derived_values_update_without_rewriting_plan(self):
        self.plan('2026-10-22', [task()])
        self.plan('2026-10-23', [task('research', 120, section='research', course_key='research', required=False)])
        before = planner.view(self.root, '2026-10-23')['derived']
        planner.set_state(self.root, '2026-10-22', 'root', 'done')
        planner.set_state(self.root, '2026-10-23', 'research', 'half')
        after = planner.view(self.root, '2026-10-23')['derived']
        self.assertEqual(before['debt']['total_hours'], 1.5)
        self.assertEqual(after['debt']['total_hours'], 0)
        self.assertEqual(after['research_week'], {'done_hours': 1.0, 'target_hours': 15.0})
        self.assertEqual(after['week_progress'][0]['exercise'], 'done')
        self.assertEqual(after['week_progress'][0]['counts']['exercise'], {'done': 1, 'half': 0, 'total': 1})

    def test_week_progress_counts_show_every_tick(self):
        self.plan('2026-10-26', [task('a', 60), task('b', 60), task('c', 60), task('lec', 60, kind='lecture')])
        planner.set_state(self.root, '2026-10-26', 'a', 'done')
        planner.set_state(self.root, '2026-10-26', 'b', 'half')
        row = planner.view(self.root, '2026-10-26')['derived']['week_progress'][0]
        # Still "half" as a whole, but the counts move with each tick.
        self.assertEqual(row['exercise'], 'half')
        self.assertEqual(row['counts']['exercise'], {'done': 1, 'half': 1, 'total': 3})
        self.assertEqual(row['counts']['lecture'], {'done': 0, 'half': 0, 'total': 1})
        self.assertEqual(row['counts']['extension'], {'done': 0, 'half': 0, 'total': 0})

    def test_week_progress_keeps_informatics_and_programming_apart(self):
        self.plan('2026-10-26', [task('iti', 60), task('fop', 60, course_key='INHN0002', course='编程基础 · Java')])
        rows = planner.view(self.root, '2026-10-26')['derived']['week_progress']
        self.assertEqual([(row['course_key'], row['course']) for row in rows],
                         [('INHN0001', '信息学导论'), ('INHN0002', '编程基础')])

    def test_berlin_midnight_schedule_and_existing_today(self):
        self.assertEqual(runtime.choose_target(self.root, now=instant('2026-10-23T21:29:00+00:00')), '2026-10-23')
        self.assertEqual(runtime.choose_target(self.root, now=instant('2026-10-23T21:30:00+00:00')), '2026-10-24')
        self.assertEqual(runtime.choose_target(self.root, now=instant('2026-10-23T22:10:00+00:00')), '2026-10-24')
        self.plan('2026-10-23', [task()])
        self.assertEqual(runtime.choose_target(self.root, now=instant()), '2026-10-24')
        with self.assertRaises(planner.PlanError):
            runtime.choose_target(self.root, target_date='2026-10-22', now=instant())

    def test_berlin_spring_and_autumn_transition_use_named_zone(self):
        cases = [('2026-03-29T00:30:00+00:00', 1, 1), ('2026-03-29T01:30:00+00:00', 3, 2),
                 ('2026-10-25T00:30:00+00:00', 2, 2), ('2026-10-25T01:30:00+00:00', 2, 1)]
        for text, hour, offset in cases:
            result = runtime.berlin_now(instant(text))
            self.assertEqual((result.hour, result.utcoffset().total_seconds() / 3600), (hour, offset))

    def test_prepare_resume_keeps_identity_draft_and_stages(self):
        first = runtime.prepare(self.root, target_date='2026-10-24', now=instant())
        path = Path(first['draft_path'])
        path.write_text('draft content survives interruption', encoding='utf-8')
        runtime.stage(self.root, '2026-10-24', 'practice', data={'identity': first['practice_identity'], 'folder': 'same-folder'}, now=instant())
        second = runtime.prepare(self.root, target_date='2026-10-24', now=instant())
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertTrue(second['resumed'])
        self.assertEqual(second['practice']['folder'], 'same-folder')
        self.assertEqual(path.read_text(encoding='utf-8'), 'draft content survives interruption')

    def test_stage_rejects_credentials_and_redacts_encoded_json_errors(self):
        runtime.prepare(self.root, target_date='2026-10-24', now=instant())
        for payload in ({'nested': {'access_token': 'private-value'}},
                        {'url': 'https://example.invalid/?sesskey%253Dprivate-value'}):
            with self.assertRaises(planner.PlanError):
                runtime.stage(self.root, '2026-10-24', 'moodle', data=payload, now=instant())
        result = runtime.stage(self.root, '2026-10-24', 'moodle',
                               error='%257B%2522sesskey%2522%253A%2522private-value%2522%257D', now=instant())
        self.assertFalse(result['ok'])
        self.assertNotIn('private-value', json.dumps(result))
        self.assertNotIn('private-value', (self.root / '状态.json').read_text(encoding='utf-8'))
        path, data = self.draft()
        data['notices'] = [{'text': 'cookie=private-value'}]
        self.assertTrue(any('凭据' in problem for problem in planner.check_plan(self.root, '2026-10-24', data=data)))

    def test_previous_hides_globally_resolved_root_without_changing_old_tick(self):
        self.plan('2026-10-22', [task()])
        self.plan('2026-10-23', [task('copy', 90, origin='debt', carry_of='2026-10-22/root',
                                       work_span={'start': 0, 'end': 5400})])
        planner.set_state(self.root, '2026-10-23', 'copy', 'done')
        data = planner.view(self.root, '2026-10-23')
        self.assertIsNone(data['previous'])
        self.assertEqual(planner.view(self.root, '2026-10-22')['states'], {})

    def test_invalid_draft_never_replaces_formal_plan(self):
        old = self.plan('2026-10-24', [task('existing', 60)])
        path, data = self.draft(tasks=[task(minutes=200)])  # Saturday maximum is 180.
        with self.assertRaises(planner.PlanError):
            runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertEqual(planner.load_plan(self.root, '2026-10-24'), old)

    def test_publish_then_repeat_is_idempotent_and_checked_task_changes_blocked(self):
        path, data = self.draft()
        result = runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertTrue(result['published'])
        self.assertTrue(runtime.publish(self.root, '2026-10-24', path, now=instant())['unchanged'])
        planner.set_state(self.root, '2026-10-24', 'root', 'done')
        data['tasks'][0]['title'] = 'different work'
        planner._write_json(path, data)
        with self.assertRaises(planner.PlanError):
            runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertEqual(planner.load_plan(self.root, '2026-10-24')['tasks'][0]['title'], 'root')

    def test_publish_atomic_failure_preserves_previous_file_and_remains_resumable(self):
        self.plan('2026-10-24', [task('old')])
        path, _ = self.draft()
        old = (self.root / '计划' / '2026-10-24.json').read_bytes()
        with patch.object(planner.os, 'replace', side_effect=OSError('simulated interrupted replace')):
            with self.assertRaises(OSError):
                runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertEqual((self.root / '计划' / '2026-10-24.json').read_bytes(), old)
        self.assertFalse(runtime.prepare(self.root, target_date='2026-10-24', now=instant())['published'])

    def test_receipt_failure_recovers_from_formal_run_id_and_blocks_changed_draft(self):
        path, data = self.draft()
        original_write = planner._write_json
        def fail_receipt(target, value):
            if Path(target).parent.name == '运行' and value.get('published'):
                raise OSError('simulated receipt failure')
            return original_write(target, value)
        with patch.object(planner, '_write_json', side_effect=fail_receipt):
            with self.assertRaises(OSError):
                runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertEqual(planner.load_plan(self.root, '2026-10-24')['run_id'], data['run_id'])
        self.assertTrue(runtime.prepare(self.root, target_date='2026-10-24', now=instant())['published'])
        self.assertTrue(planner.load_status(self.root)['ok'])
        data['tasks'][0]['title'] = 'new draft after committed publish'
        planner._write_json(path, data)
        with self.assertRaises(planner.PlanError):
            runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertEqual(planner.load_plan(self.root, '2026-10-24')['tasks'][0]['title'], 'root')

    def test_publish_readback_mismatch_cannot_report_success(self):
        path, _ = self.draft()
        original_write = planner._write_json
        def altered_formal(target, value):
            if Path(target).parent.name == '计划':
                value = {**value, 'run_id': 'unexpected-other-run'}
            return original_write(target, value)
        with patch.object(planner, '_write_json', side_effect=altered_formal):
            with self.assertRaisesRegex(planner.PlanError, '发布后复查'):
                runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertFalse(planner.load_status(self.root)['ok'])
        receipt = json.loads((self.root / '运行' / '2026-10-24.json').read_text(encoding='utf-8'))
        self.assertFalse(receipt['published'])
        self.assertFalse(receipt['stages']['publish']['ok'])

    def test_success_status_is_repaired_after_final_receipt_failure(self):
        path, _ = self.draft()
        original_write = planner._write_json
        def fail_status(target, value):
            if Path(target).name == planner.STATUS_FILE and value.get('ok') is True:
                raise OSError('simulated final status failure')
            return original_write(target, value)
        with patch.object(planner, '_write_json', side_effect=fail_status):
            with self.assertRaises(OSError):
                runtime.publish(self.root, '2026-10-24', path, now=instant())
        self.assertIsNone(planner.load_status(self.root)['ok'])
        self.assertTrue(runtime.publish(self.root, '2026-10-24', path, now=instant())['unchanged'])
        self.assertTrue(planner.load_status(self.root)['ok'])

    def test_v2_root_references_spans_materials_courses_and_special_day(self):
        self.plan('2026-10-22', [task()])
        path, data = self.draft(tasks=[task('carry', 50, origin='debt', carry_of='2026-10-22/root',
                                           work_span={'start': 0, 'end': 3000})])
        self.assertEqual(planner.check_plan(self.root, '2026-10-24', data=data), [])
        data['tasks'][0]['work_span']['end'] = 6000
        self.assertTrue(any('片段' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data['tasks'][0]['work_span']['end'] = 3000
        data['tasks'][0]['minutes'] = 1
        self.assertTrue(any('片段秒数' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data['tasks'][0]['minutes'] = 50
        duplicate = dict(data['tasks'][0], id='duplicate')
        data['tasks'].append(duplicate)
        self.assertTrue(any('不能重叠' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data['tasks'].pop()
        data['tasks'][0]['carry_of'] = '2026-10-21/missing'
        self.assertTrue(any('真实原始任务' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data['tasks'] = [task(course_key='UNKNOWN')]
        self.assertTrue(any('课程表' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data['tasks'] = [task(materials=[{'path': str(self.root / 'absent.pdf'), 'pages': [0, 2]}])]
        self.assertTrue(any('材料路径' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        data.update(date='2026-10-25', tasks=[])
        self.assertEqual(planner.check_plan(self.root, '2026-10-25', data=data), [])

    def test_material_hash_and_read_ranges_are_required_for_indexed_refs(self):
        import hashlib
        materials = planner.sibling('study_materials')
        file = self.root / 'slides.pdf'
        file.write_bytes(b'fictional PDF for local material tests')
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        report = {'dry_run': False, 'course_id': 1, 'artifacts': [{'identity': '1/1/slides.pdf',
                  'course_id': 1, 'cmid': 1, 'url': 'https://www.moodle.tum.de/pluginfile.php/a.pdf',
                  'local_path': file.name, 'sha256': sha, 'size': file.stat().st_size}], 'errors': []}
        materials.ingest(self.root, report, self.root, 'moodle', 'INHN0001')
        key = materials.pending(self.root)['items'][0]['id']
        materials.record_summary(self.root, key, 'first ten pages checked', pages=[1, 10], page_count=20)
        path, data = self.draft(tasks=[task(materials=[{'id': key, 'path': str(file), 'version': sha, 'pages': [1, 10]}])])
        self.assertEqual(planner.check_plan(self.root, '2026-10-24', data=data), [])
        ref = data['tasks'][0]['materials'][0]
        ref['pages'] = [11, 20]
        self.assertTrue(any('尚未完成材料解读' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        ref.pop('pages')
        self.assertTrue(any('必须指定' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))
        ref.pop('version')
        self.assertTrue(any('SHA256' in error for error in planner.check_plan(self.root, '2026-10-24', data=data)))

    def test_explicit_revision_backs_up_preserves_ticks_and_is_idempotent(self):
        day = '2026-10-24'
        _, old = self.publish_day(day, [task('done', 30), task('reading', 30)])
        planner.set_state(self.root, day, 'done', 'done', actual_minutes=25)
        progress_path = self.root / '进度.json'
        progress = progress_path.read_bytes()
        candidate = json.loads(json.dumps(old))
        candidate['tasks'][1]['detail'] = '按教师回复更正。'
        candidate['notices'] = [{'text': '时间尚未分配。'}]
        candidate['priority_tasks'] = ['reading']
        path = self.root / 'revision.json'
        planner._write_json(path, candidate)
        result = runtime.revise(self.root, day, path, reason='按老师公告核对', now=instant())
        current = planner.load_plan(self.root, day)
        self.assertEqual(json.loads(Path(result['backup']).read_text(encoding='utf-8')), old)
        self.assertEqual(current['run_id'], old['run_id'])
        self.assertEqual(current['generated_at'], old['generated_at'])
        self.assertEqual(current['tasks'][0], old['tasks'][0])
        self.assertEqual(current['priority_tasks'], ['reading'])
        self.assertEqual(progress_path.read_bytes(), progress)
        self.assertTrue(runtime.revise(self.root, day, path, reason='重复执行', now=instant())['unchanged'])
        self.assertEqual(len(list((self.root / '备份').glob('*.json'))), 1)

    def test_revision_rejects_work_identity_time_changes_and_invalid_priorities(self):
        day = '2026-10-24'
        _, old = self.publish_day(day, [task('reading', 30)])
        formal = self.root / '计划' / f'{day}.json'
        original = formal.read_bytes()
        for change in ('minutes', 'run_id', 'id', 'priority', 'unsafe'):
            candidate = json.loads(json.dumps(old))
            if change == 'minutes': candidate['tasks'][0]['minutes'] = 40
            if change == 'run_id': candidate['run_id'] = 'other'
            if change == 'id': candidate['tasks'][0]['id'] = 'other'
            if change == 'priority': candidate['priority_tasks'] = ['missing']
            if change == 'unsafe': candidate['notices'] = [{'text': 'cookie=private-value'}]
            path = self.root / 'revision.json'
            planner._write_json(path, candidate) if change != 'unsafe' else path.write_text(json.dumps(candidate), encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(planner.PlanError):
                runtime.revise(self.root, day, path, reason='核对', now=instant())
            self.assertEqual(formal.read_bytes(), original)
        self.assertFalse((self.root / '备份').exists())

    def test_revision_does_not_change_checked_task_or_write_on_invalid_reason(self):
        day = '2026-10-24'
        _, old = self.publish_day(day, [task('reading', 30)])
        planner.set_state(self.root, day, 'reading', 'half')
        old['tasks'][0]['detail'] = 'changed'
        path = self.root / 'revision.json'
        planner._write_json(path, old)
        for reason in ('', '核对'):
            with self.assertRaises(planner.PlanError):
                runtime.revise(self.root, day, path, reason=reason, now=instant())
        self.assertNotIn('detail', planner.load_plan(self.root, day)['tasks'][0])
        self.assertEqual(planner.load_progress(self.root)['days'][day]['reading']['state'], 'half')

    def test_tentative_upcoming_accepts_text_but_not_invalid_explicit_dates(self):
        _, data = self.draft()
        data['upcoming'] = [{'date_text': '11月底至12月初', 'title': '短测日期未定'}]
        self.assertEqual(planner.check_plan(self.root, '2026-10-24', data=data), [])
        for item in ({'date_text': '', 'title': 'x'}, {'date': 'soon', 'date_text': '待定', 'title': 'x'}):
            data['upcoming'] = [item]
            self.assertTrue(any('upcoming' in p for p in planner.check_plan(self.root, '2026-10-24', data=data)))

    def test_settings_is_single_source_and_unparseable_exception_is_rejected(self):
        settings = planner.sibling('study_settings').load_settings(self.root / '规划设定.md')
        self.assertEqual(settings['courses']['INHN0002']['ratio_group'], 'INHN0001')
        self.assertEqual(settings['research_week_minutes'], 900)
        path = self.root / '规划设定.md'
        path.write_text(path.read_text(encoding='utf-8') + '| 2026-10-26 | 忙 | 少一点 |\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            planner.sibling('study_settings').load_settings(path)


if __name__ == '__main__':
    unittest.main()
