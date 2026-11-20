"""Local files of the nightly study plan: plans written by the scheduled task, ticks written by the web page.

    <study root>\\Planner\\   (the folder above this project, or TUM_STUDY_ROOT)
      计划\\2026-10-23.json   one plan per day (written by the nightly task, read here)
      进度.json              the user's ticks: done / half / no / dropped per task
      状态.json              result of the last nightly run (written by the nightly task)

Nothing here talks to Moodle or Artemis. Plans are only read; the progress file is
the only file this module writes, always as a whole via a temporary file.

The nightly task uses two read-only commands:

    python -I planner.py debt --before 2026-10-23   # unfinished required work, in hours
    python -I planner.py validate 2026-10-24        # check a plan it has just written
"""

from __future__ import annotations

import json
import importlib.util
import math
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

# The study root holds 课程对照表.md and Planner\; by default the folder that contains this project.
STUDY_ROOT = Path(os.environ.get("TUM_STUDY_ROOT") or Path(__file__).resolve().parent.parent)
DEFAULT_DIR = STUDY_ROOT / "Planner"
PLAN_DIR = "计划"
PROGRESS_FILE = "进度.json"
STATUS_FILE = "状态.json"
STATES = ("done", "half", "no", "dropped")
FINISHED = ("done", "dropped")
UNDERSTANDING = ("understood", "hard", "not_understood")
_UNSET = object()

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_lock = threading.Lock()
_PRIVATE_NAMES = r'(?:password|passwd|sesskey|MoodleSession\w*|jwt|access_token|refresh_token|authorization|cookie|totp_secret|api_key|runtime_key|token|secret|SAMLResponse)'
_PRIVATE_KEY = re.compile(_PRIVATE_NAMES, re.I)
_PRIVATE_VALUE = re.compile(r'(["\']?\b' + _PRIVATE_NAMES + r'\b["\']?\s*[:=]\s*)("[^"]*"|\'[^\']*\'|Bearer\s+[^\s,;}\]]+|[^\s,;}\]]+)', re.I)


class PlanError(Exception):
    """A readable problem with the planner files or a request; safe to show."""


def _decoded(value):
    for _ in range(2):
        value = unquote(value)
    return value


def redact_sensitive(value):
    return _PRIVATE_VALUE.sub(lambda match: match.group(1) + '***', _decoded(str(value)))


def assert_safe_data(data):
    """Reject obvious credential payloads; never echo the rejected key/value."""
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and _PRIVATE_KEY.fullmatch(_decoded(key).replace('-', '_')):
                raise PlanError('内容含凭据字段，已拒绝写入。')
            assert_safe_data(value)
    elif isinstance(data, (list, tuple)):
        for value in data:
            assert_safe_data(value)
    elif isinstance(data, str):
        for match in _PRIVATE_VALUE.finditer(_decoded(data)):
            if match.group(2).strip('"\'') != '***':
                raise PlanError('内容含疑似会话凭据，已拒绝写入。')


def sibling(name):
    """Load a project module even when Python -I omits the script directory."""
    key = "_tum_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return sys.modules[key]


@contextmanager
def planner_lock(root):
    with _lock:
        locks = sibling("local_lock")
        try:
            with locks.directory_lock(root):
                yield
        except locks.DirectoryBusyError as exc:
            raise PlanError(str(exc)) from None


def berlin_now(now=None):
    return sibling("planner_runtime").berlin_now(now)


def valid_date(value) -> str:
    if not isinstance(value, str) or not _DATE.match(value):
        raise PlanError("日期格式应为 YYYY-MM-DD。")
    try:
        Date.fromisoformat(value)
    except ValueError:
        raise PlanError("日期不存在。")
    return value


def plan_dates(root) -> list[str]:
    folder = Path(root) / PLAN_DIR
    if not folder.is_dir():
        return []
    dates = []
    for path in folder.glob("*.json"):
        try:
            dates.append(valid_date(path.stem))
        except PlanError:
            continue
    return sorted(dates)


def _read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        raise PlanError(f"{what}无法读取或已损坏：{path.name}")


def load_plan(root, day: str) -> dict | None:
    """The plan of one day, or None. Task ids must be unique; tasks need an id and a title."""
    data = _read_json(Path(root) / PLAN_DIR / f"{valid_date(day)}.json", "计划文件")
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise PlanError(f"计划文件格式不认识：{day}.json")
    if type(data.get('version', 1)) is not int or data.get('version', 1) not in (1, 2):
        raise PlanError(f"计划文件版本不认识：{day}.json")
    if data.get('version') == 2 and data.get('date') != day:
        raise PlanError(f"计划日期与文件名不一致：{day}.json")
    seen = set()
    for task in data["tasks"]:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str) or not _TASK_ID.match(task["id"])
                or task["id"] in seen or not isinstance(task.get("title"), str)):
            raise PlanError(f"计划文件里有无效或重复的任务：{day}.json")
        seen.add(task["id"])
    data["date"] = day
    return data


def load_progress(root) -> dict:
    data = _read_json(Path(root) / PROGRESS_FILE, "进度文件")
    if data is None:
        return {"version": 1, "days": {}}
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("days"), dict):
        raise PlanError("进度文件格式不认识，已停止写入以免覆盖。")
    for day, entries in data['days'].items():
        valid_date(day)
        if not isinstance(entries, dict):
            raise PlanError('进度文件的日期记录已损坏，已停止写入。')
        for task_id, entry in entries.items():
            if (not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id) or not isinstance(entry, dict)
                    or ('state' in entry and entry['state'] not in STATES)):
                raise PlanError('进度文件的任务记录已损坏，已停止写入。')
    return data


def load_status(root) -> dict | None:
    try:
        data = _read_json(Path(root) / STATUS_FILE, "状态文件")
    except PlanError as exc:
        return {"ok": False, "message": str(exc)}
    return data if isinstance(data, dict) else None


def _day_states(progress: dict, day: str) -> dict:
    entries = progress["days"].get(day)
    if not isinstance(entries, dict):
        return {}
    return {k: v["state"] for k, v in entries.items() if isinstance(v, dict) and v.get("state") in STATES}


def set_state(root, day: str, task_id, state, *, actual_minutes=_UNSET, understanding=_UNSET) -> dict:
    """Record one tick; state None clears it. Only tasks of an existing plan; "dropped" only for debt."""
    if not isinstance(task_id, str) or not _TASK_ID.match(task_id):
        raise PlanError("任务编号无效。")
    if state is not None and state not in STATES:
        raise PlanError("状态只能是 done、half、no、dropped 或清除。")
    if actual_minutes is not _UNSET and actual_minutes is not None:
        if type(actual_minutes) is not int or not 1 <= actual_minutes <= 1440:
            raise PlanError("实际耗时必须是 1 到 1440 之间的整数分钟数。")
    if understanding is not _UNSET and understanding is not None and understanding not in UNDERSTANDING:
        raise PlanError("理解情况只能是 understood、hard、not_understood 或清除。")
    root = Path(root)
    with planner_lock(root):
        plan = load_plan(root, day)
        if plan is None:
            raise PlanError("这一天没有计划。")
        task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
        if task is None:
            raise PlanError("计划里没有这个任务。")
        if state == "dropped" and task.get("origin") != "debt":
            raise PlanError("只有欠账任务可以选择“不再补”。")
        progress = load_progress(root)
        entries = progress["days"].setdefault(day, {})
        entry = dict(entries.get(task_id) or {})
        if state is None:
            entry.pop("state", None)
        else:
            entry["state"] = state
        for key, value in (("actual_minutes", actual_minutes), ("understanding", understanding)):
            if value is _UNSET:
                continue
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        if any(key in entry for key in ("state", "actual_minutes", "understanding")):
            entry["at"] = berlin_now().isoformat(timespec="seconds")
            entries[task_id] = entry
        else:
            entries.pop(task_id, None)
        if not entries:
            progress["days"].pop(day, None)
        _write_json(root / PROGRESS_FILE, progress)
        return _day_states(progress, day)


def _write_json(path: Path, data):
    assert_safe_data(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def view(root, day: str | None = None, today: Date | None = None) -> dict:
    """Everything the plan page shows for one day.

    Without a day: today's plan, else the latest earlier one. "previous" lists the
    unfinished tasks of the plan before it, which the page offers to tick: the
    nightly task plans tomorrow at 23:30, before today is over.
    """
    root = Path(root)
    today_text = (today or berlin_now().date()).isoformat()
    result = {"root": str(root), "today": today_text, "dates": [], "calendar": [], "date": None, "plan": None,
              "states": {}, "feedback": {}, "derived": {}, "previous": None, "status": None, "error": None}
    if not root.is_dir():
        result["error"] = f"还没有规划文件夹：{root}。每晚的任务第一次运行时会创建它。"
        return sibling('planner_presentation').decorate(root, result, sys.modules[__name__])
    result["status"] = load_status(root)
    dates = result["dates"] = plan_dates(root)
    if day is None:
        earlier = [d for d in dates if d <= today_text]
        day = earlier[-1] if earlier else (dates[0] if dates else None)
    else:
        valid_date(day)
    if day is None:
        return sibling('planner_presentation').decorate(root, result, sys.modules[__name__])
    result["date"] = day
    progress = load_progress(root)
    result["calendar"] = calendar(root, dates, progress)
    result["plan"] = load_plan(root, day)
    result["states"] = _day_states(progress, day)
    result["feedback"] = _feedback(progress, day)
    result["derived"] = derived(root, day, progress=progress)
    before = [d for d in dates if d < day]
    if before and Date.fromisoformat(day) - Date.fromisoformat(before[-1]) <= timedelta(days=3):
        previous_plan = load_plan(root, before[-1])
        states = _day_states(progress, before[-1])
        # Work already carried into this day's plan is ticked there, not listed twice.
        carried = {task.get("carry_of") for task in (result["plan"] or {}).get("tasks", []) if isinstance(task, dict)}
        open_tasks = [t for t in previous_plan["tasks"] if states.get(t["id"]) not in FINISHED
                      and not result['derived']['task_remaining'].get(f'{before[-1]}/{t["id"]}', {}).get('resolved')
                      and (t.get("carry_of") or f'{before[-1]}/{t["id"]}') not in carried]
        if open_tasks:
            # An inventory (kind "inventory") is long; the page only links to it.
            result["previous"] = {"date": before[-1], "tasks": open_tasks,
                                  "states": {t["id"]: states[t["id"]] for t in open_tasks if t["id"] in states},
                                  "feedback": _feedback(progress, before[-1]),
                                  "inventory": previous_plan.get("kind") == "inventory"}
    return sibling('planner_presentation').decorate(root, result, sys.modules[__name__])


def calendar(root, dates, progress) -> list[dict]:
    """One line per plan file for the page's calendar: is it the inventory, its title, how far it is ticked.

    The inventory (kind "inventory") is shown on its own, not as that date's plan.
    """
    entries = []
    for day in dates:
        plan = load_plan(root, day)
        states = _day_states(progress, day)
        title = plan.get("title")
        scheduled = [task for task in plan['tasks'] if not task.get('auto_carry')]
        entries.append({"date": day, "inventory": plan.get("kind") == "inventory",
                        "title": title if isinstance(title, str) else None, "tasks": len(plan["tasks"]),
                        "done": sum(1 for t in plan["tasks"] if states.get(t["id"]) in FINISHED),
                        "half": sum(1 for t in plan["tasks"] if states.get(t["id"]) == "half"),
                        "planned_tasks": len(scheduled),
                        "planned_done": sum(1 for t in scheduled if states.get(t['id']) == 'done')})
    return entries


def _feedback(progress, day):
    return {key: {name: value for name, value in entry.items()
                  if name in ("actual_minutes", "understanding", "at")}
            for key, entry in progress["days"].get(day, {}).items()
            if isinstance(entry, dict) and any(name in entry for name in ("actual_minutes", "understanding"))}


def _merge(spans):
    merged = []
    for start, end in sorted(spans, key=lambda pair: (pair[0], pair[1])):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def _remaining(start, end, covered):
    result, cursor = [], start
    for left, right in covered:
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            result.append([cursor, min(left, end)])
        cursor = max(cursor, right)
    if cursor < end:
        result.append([cursor, end])
    return result


def _span(task, budget):
    span = task.get("work_span")
    if span is None:
        # Legacy carries represent a prefix, never a replacement of the root budget.
        return 0, min(budget, int(task.get("minutes") or 0) * 60)
    if (not isinstance(span, dict) or type(span.get("start")) is not int or type(span.get("end")) is not int
            or not 0 <= span["start"] < span["end"] <= budget):
        raise PlanError("工作片段无效或超出原始任务范围。")
    return span["start"], span["end"]


def _ledger(root, progress=None):
    """Fixed root budgets plus union of completed effort intervals. No state machine."""
    records, groups = {}, {}
    for day in plan_dates(root):
        for task in load_plan(root, day)["tasks"]:
            if task.get("section") != "research":
                records[f"{day}/{task['id']}"] = (day, task)
    progress = progress or load_progress(root)
    for own, (day, task) in records.items():
        key = task.get("carry_of") or own
        visited = {own}
        while key in records and records[key][1].get("carry_of"):
            if key in visited:
                raise PlanError("欠账引用形成了循环。")
            visited.add(key)
            key = records[key][1]["carry_of"]
        root_task = records[key][1] if key in records else task
        # A missing legacy origin uses its first available copy, not the latest copy.
        if key not in groups:
            budget = int(root_task.get("minutes") or 0) * 60
            groups[key] = {"task": root_task, "budget": budget, "covered": [], "dropped": False,
                           "copies": [], "missing_root": key not in records}
        group = groups[key]
        start, end = _span(task, group["budget"])
        entry = progress["days"].get(day, {}).get(task["id"], {})
        state = entry.get("state") if isinstance(entry, dict) else None
        if state == "dropped":
            group["dropped"] = True
        if state in ("done", "half"):
            group["covered"].append([start, end if state == "done" else start + (end - start) // 2])
        group["copies"].append({"key": own, "day": day, "task": task, "state": state, "span": [start, end]})
    for group in groups.values():
        group["covered"] = _merge(group["covered"])
        group["remaining"] = [] if group["dropped"] else _remaining(0, group["budget"], group["covered"])
        group["seconds_left"] = sum(end - start for start, end in group["remaining"])
    return groups


def debt(root, before: str, *, progress=None, ledger=None) -> dict:
    """Root items dated before the cutoff; all known ticks may complete those old roots."""
    valid_date(before)
    groups = ledger if ledger is not None else _ledger(root, progress)
    items = []
    for key, group in groups.items():
        task = group["task"]
        if key.split('/')[0] >= before or task.get("required") is False or not group["seconds_left"]:
            continue
        if group['missing_root'] and min(copy['day'] for copy in group['copies']) >= before:
            continue  # Do not create historical debt from an orphan first seen in a future plan.
        items.append({"key": key, "since": key.split("/")[0], "title": task["title"],
                      "course": task.get("course"), "course_key": task.get("course_key"),
                      "kind": task.get("kind"), "minutes_left": group["seconds_left"] / 60,
                      "seconds_left": group["seconds_left"], "original_minutes": group["budget"] / 60,
                      "remaining_spans": [{"start": a, "end": b} for a, b in group["remaining"]],
                      "last_state": group["copies"][-1]["state"], "missing_root": group["missing_root"]})
    courses = {}
    for item in items:
        entry = courses.setdefault(item["course_key"], {"course": item["course"], "course_key": item["course_key"],
                                                        "hours": 0.0})
        entry["hours"] += item["minutes_left"] / 60
    by_course = sorted(({**c, "hours": round(c["hours"], 2)} for c in courses.values()), key=lambda c: -c["hours"])
    return {"before": before, "total_hours": round(sum(i["minutes_left"] for i in items) / 60, 2),
            "by_course": by_course, "items": sorted(items, key=lambda i: (i["since"], i["key"]))}


def allocate(root, key, minutes, *, target_date):
    valid_date(target_date)
    if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0:
        raise PlanError("预计分钟数必须大于零。")
    group = _ledger(root).get(key)
    if not group or group["missing_root"]:
        raise PlanError("找不到真实原始任务，请直接引用原始任务的日期/编号。")
    if key.split('/')[0] >= target_date:
        raise PlanError("欠账必须引用目标日之前的原始任务。")
    if not group["remaining"]:
        raise PlanError("这个原始任务已经完成或已不再补。")
    # Avoid allocating the same range twice inside an existing target-day draft/plan.
    unavailable = list(group["covered"])
    for copy in group["copies"]:
        if copy["day"] == target_date:
            unavailable.append(copy["span"])
    draft = _read_json(Path(root) / '草稿' / f'{target_date}.json', '计划草稿')
    if draft is not None:
        if not isinstance(draft, dict) or not isinstance(draft.get('tasks'), list):
            raise PlanError('计划草稿格式不认识，未分配新片段。')
        for task in draft['tasks']:
            if isinstance(task, dict) and task.get('carry_of') == key:
                unavailable.append(_span(task, group['budget']))
    remaining = _remaining(0, group["budget"], _merge(unavailable))
    if not remaining:
        raise PlanError("目标日已经安排了这个原始任务的全部剩余片段。")
    start, limit = remaining[0]
    end = min(limit, start + max(1, int(minutes * 60)))
    return {"carry_of": key, "work_span": {"start": start, "end": end},
            "minutes": math.ceil((end - start) / 60), "target_date": target_date}


CARRY_PREFIX = "c-"
URGENT_KINDS = ("deadline", "test_prep")


def carry_forward(root, day, plan, *, progress=None):
    """Put every unfinished required course item from before `day` into that day's plan.

    User rule (2026-10-25): unfinished work is carried to the next day, and again,
    until it is done or dropped; the day's new tasks come on top. The copies added
    here are marked auto_carry, are listed in their own 顺延 section and do not count
    against the day's course budget. Spans the plan already schedules for an item
    are kept and only the rest is added; untouched automatic copies of items finished
    or dropped meanwhile are removed. Returns (new plan, number of copies added).
    """
    valid_date(day)
    progress = progress or load_progress(root)
    ledger = _ledger(root, progress)
    ticked = {key for key, entry in progress["days"].get(day, {}).items() if isinstance(entry, dict) and entry}
    plan = json.loads(json.dumps(plan))
    kept = []
    for task in plan["tasks"]:
        if isinstance(task, dict) and task.get("auto_carry") and task.get("id") not in ticked:
            group = ledger.get(task.get("carry_of"))
            if not group or not group["remaining"]:
                continue
        kept.append(task)
    plan["tasks"] = kept
    try:
        courses = sibling("study_settings").load_settings(Path(root) / "规划设定.md")["courses"]
    except (ValueError, OSError):
        courses = None  # Legacy fixture without settings: carry every course.
    ids = {task.get("id") for task in kept if isinstance(task, dict)}
    added = []
    for key, group in ledger.items():
        task = group["task"]
        if (key.split("/")[0] >= day or group["missing_root"] or task.get("required") is False
                or task.get("section") == "research" or not group["remaining"]
                or (courses is not None and task.get("course_key") not in courses)):
            continue
        scheduled = [_span(copy, group["budget"]) for copy in kept
                     if isinstance(copy, dict) and copy.get("carry_of") == key]
        base = (CARRY_PREFIX + key.split("/", 1)[1])[:34]
        for start, end in _remaining(0, group["budget"], _merge([*group["covered"], *map(list, scheduled)])):
            task_id, number = base, 2
            while task_id in ids:
                task_id, number = f"{base}-{number}", number + 1
            ids.add(task_id)
            copy = {"id": task_id, "section": "course", "course": task.get("course"),
                    "course_key": task.get("course_key"), "kind": task.get("kind") or "exercise",
                    "required": True, "origin": "debt", "carry_of": key, "debt_from": key.split("/")[0],
                    "origin_note": "顺延", "auto_carry": True, "work_span": {"start": start, "end": end},
                    "minutes": math.ceil((end - start) / 60), "title": task["title"]}
            if isinstance(task.get("detail"), str):
                copy["detail"] = task["detail"]
            links = [link for link in task.get("links") or [] if isinstance(link, dict)
                     and isinstance(link.get("url"), str) and link["url"].startswith("https://")]
            if links:
                copy["links"] = links
            added.append(copy)
    added.sort(key=lambda copy: (copy["kind"] not in URGENT_KINDS, copy["debt_from"], copy["id"]))
    plan["tasks"].extend(added)
    return plan, len(added)


def derived(root, day, *, progress=None):
    progress = progress or load_progress(root)
    ledger = _ledger(root, progress)
    monday = Date.fromisoformat(day) - timedelta(days=Date.fromisoformat(day).weekday())
    sunday = monday + timedelta(days=6)
    courses, remaining, research_minutes = {}, {}, 0
    for key, group in ledger.items():
        for copy in group["copies"]:
            span = copy["span"]
            left = 0 if group["dropped"] else sum(b-a for a, b in _remaining(*span, group["covered"]))
            remaining[copy["key"]] = {"seconds_left": left, "minutes_left": round(left / 60, 2),
                                      "root": key, "resolved": left == 0, "dropped": group["dropped"]}
            if not monday.isoformat() <= copy["day"] <= sunday.isoformat():
                continue
            task = copy["task"]
            kind = task.get("kind")
            category = 'extension' if task.get('required') is False else 'lecture' if kind == 'lecture' else 'exercise'
            course_key = task.get("course_key") or "unknown"
            # One row per course, so each row can link to its own group on the page. Task
            # labels may add detail ("编程基础 · Java"); the row shows the course name only.
            name = (task.get('course') or course_key).split(' · ')[0]
            entry = courses.setdefault(course_key, {"course": name, "course_key": course_key,
                                                    "lecture": [], "exercise": [], "extension": []})
            entry[category].append('done' if left == 0 else 'half' if left < span[1] - span[0] else 'none')
    for plan_day in plan_dates(root):
        if not monday.isoformat() <= plan_day <= sunday.isoformat():
            continue
        states = _day_states(progress, plan_day)
        for task in load_plan(root, plan_day)["tasks"]:
            if task.get('section') == 'research':
                research_minutes += (task.get('minutes') or 0) * {'done': 1, 'half': .5}.get(states.get(task['id']), 0)
    rows = []
    for entry in courses.values():
        row = {name: entry[name] for name in ('course', 'course_key')}
        # counts: how many of the week's pieces are done / partly done, so the page can show
        # every single tick (the three-state value alone stays "half" until all are done).
        row['counts'] = {}
        for kind in ('lecture', 'exercise', 'extension'):
            values = entry[kind]
            row[kind] = 'done' if values and all(value == 'done' for value in values) else 'half' if any(value != 'none' for value in values) else 'none'
            row['counts'][kind] = {'done': values.count('done'), 'half': values.count('half'), 'total': len(values)}
        rows.append(row)
    target = 15
    try:
        target = sibling('study_settings').load_settings(Path(root) / '规划设定.md')['research_week_minutes'] / 60
    except (ValueError, OSError):
        pass  # Legacy fixture/installation without settings remains readable.
    return {'debt': debt(root, day, progress=progress, ledger=ledger), 'week_progress': rows,
            'research_week': {'done_hours': round(research_minutes / 60, 2), 'target_hours': target},
            'task_remaining': remaining}


def check_plan(root, day: str, *, data=None) -> list[str]:
    """Problems in a written plan that the page or debt() would trip over; [] when fine."""
    try:
        plan = data if data is not None else load_plan(root, day)
    except PlanError as exc:
        return [str(exc)]
    if plan is None:
        return [f"没有 {day} 的计划文件。"]
    if not isinstance(plan, dict) or not isinstance(plan.get('tasks'), list):
        return ['计划需要 tasks 列表。']
    if type(plan.get('version', 1)) is not int or plan.get('version', 1) not in (1, 2):
        return ['计划版本必须是整数1或2。']
    try:
        assert_safe_data(plan)
    except PlanError as exc:
        return [str(exc)]
    problems = []
    seen = set()
    for task in plan["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not _TASK_ID.fullmatch(task['id']):
            problems.append('任务编号无效。')
            continue
        name = task["id"]
        if name in seen:
            problems.append(f'{name}: 任务编号重复')
        seen.add(name)
        if not isinstance(task.get('title'), str) or not task['title'].strip():
            problems.append(f'{name}: 缺少任务标题')
        if task.get("section") not in ("course", "research"):
            problems.append(f"{name}: section 只能是 course 或 research")
        if type(task.get("minutes")) is not int or task["minutes"] <= 0:
            problems.append(f"{name}: minutes 必须是正整数")
        if not isinstance(task.get("required"), bool):
            problems.append(f"{name}: required 必须是 true 或 false")
        if task.get("origin") not in ("new", "debt"):
            problems.append(f"{name}: origin 只能是 new 或 debt")
        if task.get("auto_carry") and task.get("origin") != "debt":
            problems.append(f"{name}: 自动顺延的任务必须是 origin: debt")
        if task.get('due_at') is not None:
            try:
                due = datetime.fromisoformat(task['due_at'])
                if due.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f'{name}: due_at 需要带时区的截止时间')
        if task.get("origin") == "debt" and not re.match(r"^\d{4}-\d{2}-\d{2}/[A-Za-z0-9_-]{1,40}$",
                                                         str(task.get("carry_of") or "")):
            problems.append(f"{name}: 欠账任务需要 carry_of（原任务的 日期/编号）")
        practice = task.get("practice")
        if practice is not None and (not isinstance(practice, dict) or not isinstance(practice.get("folder"), str)):
            problems.append(f"{name}: practice 需要 folder")
    upcoming = plan.get('upcoming')
    if upcoming is not None and not isinstance(upcoming, list):
        problems.append('upcoming 必须是列表。')
        upcoming = []
    for item in upcoming or []:
        if (isinstance(item, dict) and item.get('date') is None
                and isinstance(item.get('date_text'), str) and item['date_text'].strip()
                and isinstance(item.get('title'), str) and item['title'].strip()):
            continue  # An explicit approximate window must not acquire a made-up date.
        try:
            valid_date(item.get("date") if isinstance(item, dict) else None)
        except PlanError:
            problems.append(f"upcoming 里有无效日期：{item!r}"[:120])
    if plan.get('version', 1) == 2:
        problems.extend(_strict_problems(root, day, plan))
    elif plan.get('version', 1) != 1:
        problems.append('计划版本必须是 1 或 2。')
    return problems


def _inside(path, roots):
    resolved = path.resolve()
    return any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def _strict_problems(root, day, plan):
    problems = []
    if plan.get('date') != day:
        problems.append('计划 date 必须与目标日期一致。')
    try:
        generated = datetime.fromisoformat(plan.get('generated_at') or '')
        if generated.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        problems.append('generated_at 需要带时区的时间。')
    for field in ('sources', 'notices'):
        if plan.get(field) is not None and not isinstance(plan[field], list):
            problems.append(f'{field} 必须是列表。')
    if isinstance(plan.get('sources'), list):
        for source in plan['sources']:
            if (not isinstance(source, dict) or not isinstance(source.get('name'), str)
                    or type(source.get('ok')) is not bool):
                problems.append('sources 每项需要名称和布尔 ok。')
    if isinstance(plan.get('notices'), list):
        for notice in plan['notices']:
            if not isinstance(notice, str) and (not isinstance(notice, dict) or not isinstance(notice.get('text'), str)):
                problems.append('notices 每项需要文字。')
    priorities = plan.get('priority_tasks', [])
    if (not isinstance(priorities, list) or len(priorities) > 3
            or any(not isinstance(key, str) for key in priorities)):
        problems.append('priority_tasks 需要最多三个任务编号。')
    elif (len(priorities) != len(set(priorities))
          or any(not any(t.get('id') == key and t.get('section') != 'research' and not t.get('auto_carry')
                         for t in plan['tasks'] if isinstance(t, dict)) for key in priorities)):
        problems.append('priority_tasks 只能指定不重复的当天课程任务。')
    settings = None
    try:
        settings = sibling('study_settings').load_settings(Path(root) / '规划设定.md')
    except (ValueError, OSError) as exc:
        problems.append(str(exc))
    allowed = [Path(root)] + [Path(item['folder']) for item in (settings or {}).get('courses', {}).values() if item.get('folder')]
    tasks_by_key = {}
    for plan_day in plan_dates(root):
        existing = plan if plan_day == day else load_plan(root, plan_day)
        tasks_by_key.update({f'{plan_day}/{task["id"]}': task for task in existing['tasks']
                             if isinstance(task, dict) and task.get('id')})
    if not plan_dates(root) or day not in plan_dates(root):
        tasks_by_key.update({f'{day}/{task["id"]}': task for task in plan['tasks'] if isinstance(task, dict) and task.get('id')})
    carried_spans = {}
    for task in plan['tasks']:
        if not isinstance(task, dict):
            continue
        name = task.get('id', '?')
        if (settings and task.get('section') == 'course'
                and (not isinstance(task.get('course_key'), str) or task['course_key'] not in settings['courses'])):
            problems.append(f'{name}: 课程不在规划设定的课程表里')
        if task.get('section') == 'research' and task.get('course_key') not in ('research', 'extracurricular'):
            problems.append(f'{name}: 科研与课外类别无效')
        if task.get('origin') == 'debt':
            if task.get('required') is not True:
                problems.append(f'{name}: 欠账任务必须 required: true')
            key = task.get('carry_of')
            original = tasks_by_key.get(key) if isinstance(key, str) else None
            if not original or original.get('carry_of') or key.split('/')[0] >= day:
                problems.append(f'{name}: carry_of 必须直接引用更早日期的真实原始任务')
            elif original.get('required') is False or original.get('section') == 'research':
                problems.append(f'{name}: 只有必做课程原项才能作为欠账')
            elif type(original.get('minutes')) is int:
                try:
                    if task.get('work_span') is None:
                        raise PlanError('v2 欠账任务需要 work_span。')
                    start, end = _span(task, original['minutes'] * 60)
                    if task.get('minutes') != math.ceil((end - start) / 60):
                        raise PlanError('minutes 必须等于片段秒数换算后向上取整的分钟数。')
                    if any(start < old_end and old_start < end for old_start, old_end in carried_spans.get(key, [])):
                        raise PlanError('同一天同一原项的工作片段不能重叠。')
                    carried_spans.setdefault(key, []).append((start, end))
                except PlanError as exc:
                    problems.append(f'{name}: {exc}')
        elif task.get('work_span') is not None and type(task.get('minutes')) is int:
            if task['work_span'] != {'start': 0, 'end': task['minutes'] * 60}:
                problems.append(f'{name}: 原始任务片段只能是整个原始预算')
        materials = task.get('materials')
        if materials is None:
            materials = []
        if not isinstance(materials, list):
            problems.append(f'{name}: materials 必须是列表')
            materials = []
        for material in materials:
            try:
                if not isinstance(material, dict):
                    raise PlanError('材料引用必须是对象。')
                file = Path(material.get('path') or '')
                if material.get('id'):
                    # The index follows a file renamed by sync (Entrepreneurship, Analysis activity names).
                    file = Path(sibling('study_materials').resolve(root, material['id'], material.get('version'))['path'])
                if not file.is_absolute() or not file.is_file() or not _inside(file, allowed):
                    raise PlanError('材料路径不存在或不在允许的课程/规划目录内。')
                if not isinstance(material.get('version'), str) or not re.fullmatch(r'[0-9a-f]{64}', material['version']):
                    raise PlanError('材料需要 version（SHA256），不能省略。')
                pages = material.get('pages')
                if pages is not None and (not isinstance(pages, list) or len(pages) != 2
                        or any(type(value) is not int for value in pages) or not 1 <= pages[0] <= pages[1]):
                    raise PlanError('材料页码必须是 [正整数起页, 结束页]。')
                if material.get('id'):
                    item = sibling('study_materials').resolve(root, material['id'], material.get('version'))
                    if Path(item['path']).resolve() != file.resolve():
                        raise PlanError('材料编号与路径不一致。')
                    if pages and item.get('page_count') and pages[1] > item['page_count']:
                        raise PlanError('材料页码超出已核对的页数。')
                    if pages and _remaining(pages[0], pages[1] + 1,
                                           _merge([[left, right + 1] for left, right in item.get('read_ranges', [])])):
                        raise PlanError('引用页段尚未完成材料解读。')
                    if not pages and item.get('status') not in ('summarized', 'planned'):
                        raise PlanError('未完整解读的材料必须指定已经解读的页段。')
                elif material.get('version'):
                    import hashlib
                    digest = hashlib.sha256()
                    with file.open('rb') as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                            digest.update(chunk)
                    if digest.hexdigest() != material['version']:
                        raise PlanError('材料版本与当前文件不一致，请重新核对。')
            except (PlanError, ValueError, OSError, KeyError) as exc:
                problems.append(f'{name}: {exc}')
        links = task.get('links')
        if links is None:
            links = []
        if not isinstance(links, list) or any(not isinstance(link, dict) or not isinstance(link.get('url'), str)
                or not link['url'].startswith('https://') for link in links):
            problems.append(f'{name}: 来源链接需要 https 地址')
        tags = task.get('tags')
        if tags is not None and (not isinstance(tags, list) or any(not isinstance(tag, str)
                and (not isinstance(tag, dict) or not isinstance(tag.get('text'), str)) for tag in tags)):
            problems.append(f'{name}: tags 必须是文字或带 text 的对象列表')
        practice = task.get('practice')
        if isinstance(practice, dict):
            folder = Path(practice.get('folder') or '')
            if not folder.is_absolute() or not folder.is_dir() or not _inside(folder, allowed):
                problems.append(f'{name}: 练习文件夹不存在或不在允许目录内')
        if (task.get('section') == 'course' and task.get('origin') != 'debt'
                and not materials and not links and not isinstance(practice, dict)):
            problems.append(f'{name}: 新课程任务需要材料或来源链接')
    if settings:
        budget = sibling('study_settings').budget_for(settings, day)
        # Automatically carried work is listed on top of the day's budget (顺延), not inside it.
        course = sum(t.get('minutes', 0) for t in plan['tasks'] if isinstance(t, dict) and not t.get('auto_carry')
                     and t.get('section') == 'course' and type(t.get('minutes')) is int)
        research = sum(t.get('minutes', 0) for t in plan['tasks'] if isinstance(t, dict)
                       and t.get('section') == 'research' and type(t.get('minutes')) is int)
        if course > budget['course_max_minutes']:
            problems.append(f'课程总时长 {course} 分钟超过当天上限 {budget["course_max_minutes"]} 分钟')
        if research > budget['research_minutes']:
            problems.append(f'科研与课外总时长 {research} 分钟超过当天预算 {budget["research_minutes"]} 分钟')
        if not plan['tasks'] and budget['course_minutes'] + budget['research_minutes']:
            problems.append('普通学习日不能发布空计划。')
    return problems


def plan_warnings(root, day, plan=None):
    plan = plan if plan is not None else load_plan(root, day)
    if plan is None:
        return []
    if plan.get('version', 1) == 1:
        return ['旧版计划保留兼容；欠账副本缺少片段元数据时只按原项前缀估算。']
    try:
        settings = sibling('study_settings').load_settings(Path(root) / '规划设定.md')
    except ValueError:
        return []
    budget = sibling('study_settings').budget_for(settings, day)
    totals = {}
    for task in plan['tasks']:
        if (isinstance(task, dict) and task.get('section') == 'course' and not task.get('auto_carry')
                and type(task.get('minutes')) is int):
            course = settings['courses'].get(task.get('course_key'))
            if course:
                key = course['ratio_group']
                totals[key] = totals.get(key, 0) + task['minutes']
    total = sum(totals.values())
    warnings = []
    if total > budget['course_minutes']:
        warnings.append(f'课程时长超过通常目标 {budget["course_minutes"]} 分钟，仍在允许上限内。')
    if total:
        for key, course in settings['courses'].items():
            if course['ratio'] is not None and abs(totals.get(key, 0) / total * 100 - course['ratio']) > 15:
                warnings.append(f'{course["name"]} 今日比例偏离起点值，结合本周和临近截止复核。')
    return warnings


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="planner.py", description="本地学习计划、执行记录与校验（不连网）")
    parser.add_argument("--root", type=Path, default=DEFAULT_DIR, help="规划文件夹")
    commands = parser.add_subparsers(dest="command", required=True)
    owed = commands.add_parser("debt", help="列出欠账（不含 --before 当天及以后）")
    owed.add_argument("--before", required=True, help="YYYY-MM-DD，通常是今天")
    checked = commands.add_parser("validate", help="检查某天的计划文件")
    checked.add_argument("date")
    feedback = commands.add_parser('feedback', help='汇总最近学习耗时和理解反馈（只读）')
    feedback.add_argument('date')
    prepared = commands.add_parser('prepare', help='准备或恢复一个目标日的规划运行')
    prepared.add_argument('--date')
    staged = commands.add_parser('stage', help='登记运行阶段及产物')
    staged.add_argument('date')
    staged.add_argument('name')
    staged.add_argument('--error')
    staged.add_argument('--data', type=Path)
    carried = commands.add_parser('carry', help='把之前没完成的必做任务全部顺延进某天（今天或以后）已发布的计划')
    carried.add_argument('date')
    allocated = commands.add_parser('allocate', help='分配一个原始任务未完成的连续片段')
    allocated.add_argument('key')
    allocated.add_argument('--minutes', type=float, required=True)
    allocated.add_argument('--date', required=True)
    published = commands.add_parser('publish', help='严格校验草稿后原子发布')
    published.add_argument('date')
    published.add_argument('--draft', type=Path, required=True)
    revised = commands.add_parser('revise', help='按用户要求备份并更正已发布计划的文字和来源，保留任务与进度')
    revised.add_argument('date')
    revised.add_argument('--draft', type=Path, required=True)
    revised.add_argument('--reason', required=True)
    materials = commands.add_parser('materials', help='本地材料队列')
    materials.add_argument('args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.command == "debt":
            result = debt(args.root, args.before)
        elif args.command == 'validate':
            problems = check_plan(args.root, args.date)
            result = {"ok": not problems, "problems": problems, 'warnings': plan_warnings(args.root, args.date)}
        elif args.command == 'allocate':
            result = allocate(args.root, args.key, args.minutes, target_date=args.date)
        elif args.command == 'feedback':
            valid_date(args.date)
            result = sibling('study_feedback').summarize(args.root, args.date)
        elif args.command == 'materials':
            return sibling('study_materials').main(args.args, root=args.root)
        else:
            runtime = sibling('planner_runtime')
            if args.command == 'prepare':
                result = runtime.prepare(args.root, target_date=args.date)
            elif args.command == 'carry':
                result = runtime.carry(args.root, args.date)
            elif args.command == 'stage':
                data = _read_json(args.data, '阶段产物') if args.data else None
                result = runtime.stage(args.root, args.date, args.name, data=data, error=args.error)
            elif args.command == 'revise':
                result = runtime.revise(args.root, args.date, args.draft, reason=args.reason)
            else:
                result = runtime.publish(args.root, args.date, args.draft)
    except (PlanError, ValueError, OSError) as exc:
        result = {"error": redact_sensitive(str(exc))}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if "error" not in result and result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
