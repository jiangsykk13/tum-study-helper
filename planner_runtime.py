"""Recoverable local nightly runs and validation-before-publication."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
_WINDOWS_ZONE = None
_PLANNER = None


def _planner():
    global _PLANNER
    if _PLANNER is not None:
        return _PLANNER
    expected = Path(__file__).with_name('planner.py').resolve()
    for name in ('planner', '_tum_planner', '__main__'):
        module = sys.modules.get(name)
        if module is not None and getattr(module, '__file__', None) and Path(module.__file__).resolve() == expected:
            _PLANNER = module
            return module
    spec = importlib.util.spec_from_file_location('_tum_planner', expected)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _PLANNER = module
    return module


def _convert_windows(utc, kernel, zone, SystemTime):
    source = SystemTime(utc.year, utc.month, 0, utc.day, utc.hour, utc.minute, utc.second, utc.microsecond // 1000)
    target = SystemTime()
    if not kernel.SystemTimeToTzSpecificLocalTimeEx(ctypes.byref(zone), ctypes.byref(source), ctypes.byref(target)):
        raise ValueError('无法换算柏林时间。')
    naive = datetime(target.year, target.month, target.day, target.hour, target.minute, target.second, utc.microsecond)
    offset = naive - utc.replace(tzinfo=None)
    return naive.replace(tzinfo=timezone(offset, 'Europe/Berlin'))


def _windows_berlin(utc):
    """Use the named Windows zone, independently of the machine's selected zone."""
    global _WINDOWS_ZONE
    if _WINDOWS_ZONE is not None:
        return _convert_windows(utc, *_WINDOWS_ZONE)
    from ctypes import wintypes
    class SystemTime(ctypes.Structure):
        _fields_ = [(name, wintypes.WORD) for name in
                    ('year', 'month', 'weekday', 'day', 'hour', 'minute', 'second', 'millisecond')]
    class DynamicZone(ctypes.Structure):
        _fields_ = [('bias', wintypes.LONG), ('standard_name', wintypes.WCHAR * 32),
                    ('standard_date', SystemTime), ('standard_bias', wintypes.LONG),
                    ('daylight_name', wintypes.WCHAR * 32), ('daylight_date', SystemTime),
                    ('daylight_bias', wintypes.LONG), ('key_name', wintypes.WCHAR * 128),
                    ('dynamic_disabled', wintypes.BOOLEAN)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    registry = ctypes.WinDLL('advapi32', use_last_error=True)
    registry.EnumDynamicTimeZoneInformation.argtypes = (wintypes.DWORD, ctypes.POINTER(DynamicZone))
    registry.EnumDynamicTimeZoneInformation.restype = wintypes.DWORD
    kernel.SystemTimeToTzSpecificLocalTimeEx.argtypes = (ctypes.POINTER(DynamicZone), ctypes.POINTER(SystemTime), ctypes.POINTER(SystemTime))
    kernel.SystemTimeToTzSpecificLocalTimeEx.restype = wintypes.BOOL
    zone = DynamicZone()
    for index in range(1024):
        result = registry.EnumDynamicTimeZoneInformation(index, ctypes.byref(zone))
        if result == 259:  # ERROR_NO_MORE_ITEMS
            break
        if result != 0:
            raise ValueError('无法读取 Windows 时区。')
        if zone.key_name == 'W. Europe Standard Time':
            _WINDOWS_ZONE = (kernel, zone, SystemTime)
            return _convert_windows(utc, *_WINDOWS_ZONE)
    raise ValueError('Windows 中没有 W. Europe Standard Time 时区。')


def berlin_now(now=None):
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError('传入时间必须带时区。')
    instant = instant.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        return instant.astimezone(ZoneInfo('Europe/Berlin'))
    except (ImportError, ZoneInfoNotFoundError):
        if os.name == 'nt':
            return _windows_berlin(instant)
        raise ValueError('系统没有 Europe/Berlin 时区数据。') from None


def _paths(root, day):
    p = _planner()
    p.valid_date(day)
    root = Path(root)
    return root / '运行' / f'{day}.json', root / '草稿' / f'{day}.json'


def _run(root, day):
    p = _planner()
    path, _ = _paths(root, day)
    run = p._read_json(path, '运行记录')
    if not isinstance(run, dict) or run.get('version') != 1 or run.get('target_date') != day or not isinstance(run.get('stages'), dict):
        raise p.PlanError('运行记录不存在或格式不认识，请先 prepare。')
    return run


def _safe_message(text):
    return _planner().redact_sensitive(text)[:500]


def choose_target(root, *, target_date=None, now=None):
    p = _planner()
    current = berlin_now(now)
    today = current.date()
    if target_date is not None:
        p.valid_date(target_date)
        if target_date < today.isoformat():
            raise p.PlanError('不能重写已经过去日期的计划。')
        return target_date
    if (current.hour, current.minute) >= (23, 30):
        return (today + timedelta(days=1)).isoformat()
    return today.isoformat() if p.load_plan(root, today.isoformat()) is None else (today + timedelta(days=1)).isoformat()


def _success_status(root, day, run, plan, current):
    p = _planner()
    previous = p.load_status(root) or {}
    reminded = list(dict.fromkeys([*run.get('reminded', []), *previous.get('reminded', []), *plan.get('reminded', [])]))
    p._write_json(Path(root) / p.STATUS_FILE, {'ok': True, 'state': 'published', 'run_at': current,
                  'plan_date': day, 'run_id': run['run_id'], 'message': '学习计划已生成。',
                  'sources': plan.get('sources', []), 'reminded': reminded})


def _recover_published(root, day, run, current):
    """The atomic formal file is authoritative, even if later receipts failed."""
    p = _planner()
    formal = p.load_plan(root, day)
    if formal and formal.get('version') == 2 and formal.get('run_id') == run['run_id']:
        if not run.get('published'):
            run.update(published=True, published_at=current, updated_at=current)
            run['stages']['publish'] = {'ok': True, 'at': current, 'recovered': True}
            p._write_json(_paths(root, day)[0], run)
        status = p.load_status(root) or {}
        if status.get('ok') is not True or status.get('run_id') != run['run_id']:
            _success_status(root, day, run, formal, current)
        return formal
    if run.get('published'):
        raise p.PlanError('发布记录与正式计划不一致，已停止覆盖；请核对文件。')
    return None


def prepare(root, *, target_date=None, now=None):
    p = _planner()
    root = Path(root)
    current = berlin_now(now)
    day = choose_target(root, target_date=target_date, now=current)
    settings = p.sibling('study_settings').load_settings(root / '规划设定.md')
    budget = p.sibling('study_settings').budget_for(settings, day)
    run_path, draft_path = _paths(root, day)
    with p.planner_lock(root):
        existing = p._read_json(run_path, '运行记录')
        resumed = existing is not None
        if resumed:
            run = _run(root, day)
        else:
            previous = p.load_status(root) or {}
            run = {'version': 1, 'run_id': uuid4().hex, 'target_date': day,
                   'created_at': current.isoformat(timespec='seconds'), 'stages': {}, 'published': False,
                   'practice_identity': 'java-' + day, 'reminded': previous.get('reminded', [])}
        _recover_published(root, day, run, current.isoformat(timespec='seconds'))
        run['updated_at'] = current.isoformat(timespec='seconds')
        p._write_json(run_path, run)
        if not draft_path.exists() and not run['published']:
            plan = p.load_plan(root, day)
            if plan is None:
                plan = {'version': 2, 'date': day, 'generated_at': current.isoformat(timespec='seconds'),
                        'run_id': run['run_id'], 'targets': {'course_hours': budget['course_minutes'] / 60},
                        'sources': [], 'notices': [], 'tasks': [], 'upcoming': []}
            else:
                plan = json.loads(json.dumps(plan))
                plan.update(version=2, run_id=run['run_id'], generated_at=current.isoformat(timespec='seconds'))
            p._write_json(draft_path, plan)
        if not run['published']:
            p._write_json(root / p.STATUS_FILE, {'ok': None, 'state': 'running', 'run_at': run['updated_at'],
                          'plan_date': day, 'run_id': run['run_id'], 'reminded': run.get('reminded', []),
                          'message': '正在生成学习计划；上次发布的计划仍然可读。'})
    practice = (run['stages'].get('practice') or {}).get('data')
    return {'ok': True, 'target_date': day, 'run_id': run['run_id'], 'draft_path': str(draft_path),
            'practice_identity': run['practice_identity'], 'practice': practice, 'resumed': resumed,
            'published': run['published'], 'stages': run['stages'], 'settings': settings, 'budget': budget}


def stage(root, day, name, *, data=None, error=None, now=None):
    p = _planner()
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name):
        raise p.PlanError('阶段名只能使用小写英文、数字、下划线和连字符。')
    if data is not None and not isinstance(data, dict):
        raise p.PlanError('阶段产物必须是 JSON 对象。')
    if data is not None:
        p.assert_safe_data(data)
    path, _ = _paths(root, day)
    current = berlin_now(now).isoformat(timespec='seconds')
    with p.planner_lock(root):
        run = _run(root, day)
        _recover_published(root, day, run, current)
        if run.get('published'):
            raise p.PlanError('该次运行已经发布，不能修改执行阶段。')
        run['stages'][name] = {'ok': error is None, 'at': current,
                               **({'data': data} if data is not None else {}),
                               **({'error': _safe_message(error)} if error is not None else {})}
        run['updated_at'] = current
        p._write_json(path, run)
        if error is not None:
            p._write_json(Path(root) / p.STATUS_FILE, {'ok': False, 'state': 'failed', 'run_at': current,
                          'plan_date': day, 'run_id': run['run_id'], 'reminded': run.get('reminded', []),
                          'message': _safe_message(error)})
    return {'ok': error is None, 'target_date': day, 'run_id': run['run_id'], 'stage': run['stages'][name]}


def _protect(old, candidate, progress, day):
    p = _planner()
    if old is None:
        return
    old_tasks = {task['id']: task for task in old['tasks']}
    new_tasks = {task['id']: task for task in candidate['tasks']}
    for task_id, entry in progress['days'].get(day, {}).items():
        if not isinstance(entry, dict) or not any(key in entry for key in ('state', 'actual_minutes', 'understanding')):
            continue
        if task_id not in old_tasks or new_tasks.get(task_id) != old_tasks[task_id]:
            raise p.PlanError(f'任务 {task_id} 已有勾选或反馈，不能删除、换编号或改变内容。')


def _publish(root, day, draft, *, now=None):
    p = _planner()
    root = Path(root)
    choose_target(root, target_date=day, now=now)
    path, _ = _paths(root, day)
    with p.planner_lock(root):
        run = _run(root, day)
        current = berlin_now(now).isoformat(timespec='seconds')
        _recover_published(root, day, run, current)
        candidate = p._read_json(Path(draft), '计划草稿')
        if not isinstance(candidate, dict) or type(candidate.get('version')) is not int or candidate.get('version') != 2:
            raise p.PlanError('只能发布 version: 2 的计划草稿。')
        if candidate.get('run_id') != run['run_id']:
            raise p.PlanError('草稿 run_id 与当前目标日运行不一致。')
        old = p.load_plan(root, day)
        if not run.get('published'):
            candidate, carried = p.carry_forward(root, day, candidate)
        if run.get('published'):
            if old != candidate:
                raise p.PlanError('该次运行已发布；重复发布只能使用完全相同的计划。')
            _success_status(root, day, run, candidate, current)
            return {'ok': True, 'published': True, 'unchanged': True, 'target_date': day, 'run_id': run['run_id']}
        problems = p.check_plan(root, day, data=candidate)
        if problems:
            raise p.PlanError('草稿未发布：' + '；'.join(problems))
        _protect(old, candidate, p.load_progress(root), day)
        # The formal file is replaced only after every validation/protection check.
        p._write_json(root / p.PLAN_DIR / f'{day}.json', candidate)
        formal = p.load_plan(root, day)
        if not formal or formal.get('run_id') != run['run_id'] or formal != candidate:
            raise p.PlanError('发布后复查没有读到一致的正式计划，不能报告成功。')
        run.update(published=True, published_at=current, updated_at=current)
        run['stages']['publish'] = {'ok': True, 'at': current}
        p._write_json(path, run)
        _success_status(root, day, run, candidate, current)
    return {'ok': True, 'published': True, 'unchanged': False, 'target_date': day, 'run_id': run['run_id'],
            'carried': carried, 'warnings': p.plan_warnings(root, day, candidate)}


def carry(root, day, *, now=None):
    """Carry unfinished work into an already published plan of today or later (also used once by hand)."""
    p = _planner()
    root = Path(root)
    choose_target(root, target_date=day, now=now)
    with p.planner_lock(root):
        plan = p.load_plan(root, day)
        if plan is None:
            raise p.PlanError('这一天还没有计划。')
        if plan.get('kind') == 'inventory':
            raise p.PlanError('开学盘点不顺延任务。')
        candidate, added = p.carry_forward(root, day, plan)
        if candidate == plan:
            return {'ok': True, 'target_date': day, 'added': 0, 'unchanged': True}
        problems = p.check_plan(root, day, data=candidate)
        if problems:
            raise p.PlanError('未更新：' + '；'.join(problems))
        _protect(plan, candidate, p.load_progress(root), day)
        p._write_json(root / p.PLAN_DIR / f'{day}.json', candidate)
    return {'ok': True, 'target_date': day, 'added': added, 'unchanged': False,
            'carried': sum(1 for task in candidate['tasks'] if task.get('auto_carry'))}


def publish(root, day, draft, *, now=None):
    try:
        return _publish(root, day, draft, now=now)
    except (ValueError, OSError, _planner().PlanError) as exc:
        try:
            stage(root, day, 'publish', error=str(exc), now=now)
        except (ValueError, OSError, _planner().PlanError):
            pass  # A published formal file will repair its receipts on the next prepare/publish.
        raise


def revise(root, day, draft, *, reason, now=None):
    """Explicit factual corrections only; preserve work, ticks and original publication."""
    p = _planner()
    root = Path(root)
    choose_target(root, target_date=day, now=now)
    if not isinstance(reason, str) or not reason.strip():
        raise p.PlanError('修订需要说明原因。')
    p.assert_safe_data(reason)
    with p.planner_lock(root):
        old = p.load_plan(root, day)
        candidate = p._read_json(Path(draft), '修订草稿')
        if not old or old.get('version') != 2 or not isinstance(candidate, dict):
            raise p.PlanError('只能修订已有的 version: 2 正式计划。')
        allowed = {'sources', 'notices', 'upcoming', 'priority_tasks', 'tasks', 'revision'}
        if ({k: v for k, v in old.items() if k not in allowed}
                != {k: v for k, v in candidate.items() if k not in allowed}):
            raise p.PlanError('修订不能改变日期、运行编号、生成时间或计划预算。')
        previous = old['tasks']
        current = candidate.get('tasks')
        if not isinstance(current, list) or len(previous) != len(current):
            raise p.PlanError('修订不能新增、删除或重排任务。')
        for before, after in zip(previous, current):
            if (not isinstance(after, dict)
                    or {k: v for k, v in before.items() if k not in ('detail', 'links')}
                    != {k: v for k, v in after.items() if k not in ('detail', 'links')}):
                raise p.PlanError('任务修订只允许更正说明和来源链接，不能改变编号、时长、资料或欠账关联。')
        candidate.pop('revision', None)
        comparable = {k: v for k, v in old.items() if k != 'revision'}
        if candidate == comparable:
            return {'ok': True, 'unchanged': True, 'target_date': day}
        problems = p.check_plan(root, day, data=candidate)
        if problems:
            raise p.PlanError('修订未发布：' + '；'.join(problems))
        _protect(old, candidate, p.load_progress(root), day)
        current_time = berlin_now(now).isoformat(timespec='seconds')
        backup = root / '备份' / f'计划修订_{day}_{uuid4().hex}.json'
        p._write_json(backup, old)
        candidate['revision'] = {'at': current_time, 'reason': reason.strip(), 'backup': str(backup)}
        p._write_json(root / p.PLAN_DIR / f'{day}.json', candidate)
        if p.load_plan(root, day) != candidate:
            raise p.PlanError('修订后读回不一致，不能报告成功；原计划备份已保留。')
    return {'ok': True, 'unchanged': False, 'target_date': day, 'run_id': old.get('run_id'),
            'backup': str(backup), 'revised_at': current_time}
