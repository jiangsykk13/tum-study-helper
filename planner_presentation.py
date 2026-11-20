"""Read-only presentation of published plans, run receipts and inherited task resources.

No recovery, publishing, compiling or file writes belong in this module.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from pathlib import Path
import re


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def target_minutes(value):
    """A numeric target or a legacy display range such as 6–8 (hours)."""
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return value * 60, value * 60
    if isinstance(value, str):
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:[–—−-]|至|~|～)?\s*(\d+(?:\.\d+)?)?\s*(?:小时)?\s*', value)
        if match:
            low, high = float(match[1]), float(match[2] or match[1])
            if 0 < low <= high < 1000:
                return low * 60, high * 60
    return None, None


def generation(root, result, p, *, now=None):
    now = p.berlin_now(now)
    root = Path(root)
    status = result.get('status') or {}
    candidates = []
    for folder in ('运行', '草稿'):
        for path in (root / folder).glob('*.json'):
            try:
                candidates.append(p.valid_date(path.stem))
            except p.PlanError:
                continue
    try:
        target = p.valid_date(status.get('plan_date'))
    except p.PlanError:
        target = max(candidates) if candidates else result['today']
    report = {'state': 'missing', 'target_date': target, 'shown_date': result.get('date'),
              'draft_available': False, 'last_activity_at': None, 'last_stage': None,
              'last_stage_at': None, 'message': '尚无正式计划。', 'today_missing': result['today'] not in result.get('dates', [])}
    run_path, draft_path = root / '运行' / f'{target}.json', root / '草稿' / f'{target}.json'
    problem = None
    try:
        run = p._read_json(run_path, '运行记录') or {}
        if not isinstance(run, dict) or (run and (run.get('version') != 1 or run.get('target_date') != target
                                                or not isinstance(run.get('stages'), dict))):
            run, problem = {}, '运行记录格式不认识。'
    except p.PlanError as exc:
        run, problem = {}, str(exc)
    try:
        formal = p.load_plan(root, target)
    except p.PlanError as exc:
        formal, problem = None, str(exc)
    activity = []
    for value in (run.get('updated_at'), status.get('run_at')):
        stamp = _instant(value)
        if stamp:
            activity.append(stamp)
    stages = [(name, stage, _instant(stage.get('at'))) for name, stage in run.get('stages', {}).items()
              if isinstance(stage, dict)]
    for _, _, stamp in stages:
        if stamp:
            activity.append(stamp)
    successful = [(name, stamp) for name, stage, stamp in stages if stage.get('ok') is True and stamp]
    if successful:
        name, stamp = max(successful, key=lambda item: item[1])
        report.update(last_stage=p.redact_sensitive(name)[:80], last_stage_at=stamp.isoformat())
    for path in (run_path, draft_path):
        try:
            if path.is_file():
                activity.append(datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo))
                if path == draft_path:
                    report['draft_available'] = True
        except OSError:
            continue
    if activity:
        report['last_activity_at'] = max(activity).isoformat(timespec='seconds')
    run_id = run.get('run_id') or status.get('run_id')
    published = bool(formal and ((run_id and formal.get('run_id') == run_id)
                                 or (not run_id and status.get('ok') is not False)))
    if published:
        report.update(state='published', message='正式计划已发布。')
        return report
    failed_stages = [(stage, stamp) for _, stage, stamp in stages if stage.get('ok') is False]
    latest_failed = max(failed_stages, key=lambda item: item[1].timestamp() if item[1] else 0, default=None)
    if problem or status.get('ok') is False or latest_failed:
        message = problem or status.get('message') or latest_failed[0].get('error') or '最近一次生成没有成功。'
        report.update(state='failed', message=p.redact_sensitive(message)[:500])
    elif run or status.get('state') == 'running' or report['draft_available']:
        stale = bool(activity and (now - max(activity)).total_seconds() > 3600)
        report.update(state='stale' if stale else 'running',
                      message='长时间未更新，可能中断。' if stale else '学习计划正在生成。')
    return report


def inherit_resources(root, result, p):
    """Fill only missing fields, following carry_of all the way to the original."""
    records = {}
    for day in result.get('dates', []):
        records.update({f'{day}/{task["id"]}': task for task in p.load_plan(root, day)['tasks']})
    for plan in (result.get('plan') or {}, result.get('previous') or {}):
        for task in plan.get('tasks', []):
            key, seen = task.get('carry_of'), set()
            while key and key not in seen:
                seen.add(key)
                original = records.get(key)
                if original is None:
                    task['resource_note'] = '找不到原任务，资料和练习入口可能不完整。'
                    key = None
                    break
                for field in ('materials', 'practice', 'due_at'):
                    if not task.get(field) and original.get(field):
                        task[field] = deepcopy(original[field])
                key = original.get('carry_of')
            if key in seen and key:
                task['resource_note'] = '原任务关联形成循环，无法继续恢复资料入口。'
            if task.get('carry_of') and not task.get('materials') and not task.get('practice') and not task.get('links'):
                task.setdefault('resource_note', '原任务未记录资料或练习入口。')


def summary(result, p):
    tasks = (result.get('plan') or {}).get('tasks', [])
    states = result.get('states') or {}
    fresh = [task for task in tasks if not task.get('auto_carry')]
    minutes = lambda items: sum(task.get('minutes', 0) for task in items if type(task.get('minutes')) in (int, float))
    completed = lambda items: sum(task.get('minutes', 0) * {'done': 1, 'half': .5}.get(states.get(task['id']), 0)
                                  for task in items if type(task.get('minutes')) in (int, float))
    # Repeated legacy copies of the same root must not inflate the separate catch-up figure.
    spans = {}
    for task in tasks:
        share = {'done': 1, 'half': .5}.get(states.get(task['id']), 0)
        if not task.get('auto_carry') or not share:
            continue
        span = task.get('work_span') or {'start': 0, 'end': (task.get('minutes') or 0) * 60}
        start, end = span['start'], span['end']
        spans.setdefault(task.get('carry_of') or task['id'], []).append([start, start + (end - start) * share])
    low, high = target_minutes((result.get('plan') or {}).get('targets', {}).get('course_hours'))
    return {'planned_minutes': minutes(fresh), 'course_minutes': minutes([t for t in fresh if t.get('section') != 'research']),
            'research_minutes': minutes([t for t in fresh if t.get('section') == 'research']),
            'completed_minutes': completed(fresh),
            'carried_completed_minutes': sum(b - a for ranges in spans.values() for a, b in p._merge(ranges)) / 60,
            'debt_hours': result.get('derived', {}).get('debt', {}).get('total_hours', 0),
            'target_min_minutes': low, 'target_max_minutes': high, 'recorded_focus_seconds': None}


def decorate(root, result, p, *, now=None):
    result['generation'] = generation(root, result, p, now=now)
    inherit_resources(root, result, p)
    # Published JSON is immutable. Resolve relocated versions only in its view.
    try:
        index = p.sibling('study_materials')._read(root)
    except (ValueError, OSError):
        index = {"items": {}}
    for plan in (result.get('plan'), result.get('previous')):
        for task in (plan or {}).get('tasks', []):
            for ref in task.get('materials') or []:
                if not isinstance(ref, dict):
                    continue
                item = index['items'].get(ref.get('id'))
                if item and ref.get('version') == item['version']:
                    ref['path'] = item['path']
    result['summary'] = summary(result, p)
    return result
