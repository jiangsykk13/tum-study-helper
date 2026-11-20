"""Pomodoro timer and focus statistics, stored next to the study plan.

    <study root>\\Planner\\专注记录.json   {version: 1, settings, active, next_mode, cycle, last_task, sessions}

The web page only sends actions (start, pause, resume, stop …); every live timestamp
is taken here from this computer's clock. The running timer is part of the file and
is settled against the real time on every request, so a reload, a closed tab or a
sleeping laptop neither loses nor doubles a record: a focus that ran out while
nobody was looking is recorded with the moment it really ended.

Rules (also what the statistics mean):
- a pomodoro counts as finished only when it reaches its planned length;
- stopping early keeps the real focus time (at least one minute), not the pomodoro;
- pauses and breaks are never focus time; breaks are timed but not recorded;
- a session crossing midnight stays one record, its minutes go to the days (and
  hours) they really fell in; daily averages divide by days with focus;
- manual entries are marked "manual", may not overlap other records and never
  count as pomodoros.

Nothing here talks to Moodle or Artemis.
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import local_lock
import planner

FILE = "专注记录.json"
MODES = ("focus", "stopwatch", "short", "long")
BREAKS = ("short", "long")
KINDS = ("pomodoro", "stopwatch", "manual")
REASONS = ("phone", "social", "tired", "errand", "other")
PERIODS = ("day", "week", "month", "year", "custom")
STOPWATCH_LIMIT = 3 * 3600  # a forgotten stopwatch stops itself after three hours
MIN_SAVED = 60              # shorter early stops are not worth a record
DEFAULT_SETTINGS = {
    "focus_minutes": 25, "short_minutes": 5, "long_minutes": 15, "long_every": 4,
    "auto_break": True, "auto_focus": False, "goal_pomodoros": 8, "goal_minutes": 240,
    "sound": True, "notify": True,
}
INT_LIMITS = {"focus_minutes": (1, 180), "short_minutes": (1, 60), "long_minutes": (1, 90),
              "long_every": (2, 12), "goal_pomodoros": (1, 30), "goal_minutes": (10, 960)}

_ID = re.compile(r"^[0-9a-f]{8,32}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_lock = threading.Lock()


class FocusError(Exception):
    """A readable problem with the focus file or a request; safe to show."""


# ---------------------------------------------------------------- time

def _utc(now=None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(text) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("naive timestamp")
    return moment.astimezone(timezone.utc)


def _local(moment: datetime, tz):
    """tz None means this computer's time zone (Berlin for the user)."""
    return moment.astimezone(tz)


def _day(text, what="日期") -> Date:
    try:
        return Date.fromisoformat(planner.valid_date(text))
    except planner.PlanError:
        raise FocusError(f"{what}格式应为 YYYY-MM-DD。") from None


# ---------------------------------------------------------------- file

def _empty() -> dict:
    return {"version": 1, "settings": dict(DEFAULT_SETTINGS), "active": None, "next_mode": "focus",
            "cycle": {"date": None, "count": 0}, "last_task": {}, "sessions": []}


def load(root) -> dict:
    path = Path(root) / FILE
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeDecodeError, ValueError):
        raise FocusError("专注记录无法读取或已损坏，已停止写入以免覆盖。")
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sessions"), list)
            or not isinstance(data.get("settings", {}), dict)
            or not isinstance(data.get("active"), (dict, type(None)))):
        raise FocusError("专注记录的格式不认识，已停止写入以免覆盖。")
    merged = _empty()
    merged.update({k: data[k] for k in ("active", "next_mode", "cycle", "last_task", "sessions") if k in data})
    for key, default in DEFAULT_SETTINGS.items():
        value = data.get("settings", {}).get(key, default)
        merged["settings"][key] = value if type(value) is type(default) else default
    if merged["active"] is not None and not _valid_active(merged["active"]):
        merged["active"] = None  # a damaged running timer is dropped; finished records stay
    if merged["next_mode"] not in MODES:
        merged["next_mode"] = "focus"
    if not isinstance(merged["cycle"], dict):
        merged["cycle"] = {"date": None, "count": 0}
    if not isinstance(merged["last_task"], dict):
        merged["last_task"] = {}
    return merged


def _save(root, data):
    try:
        planner._write_json(Path(root) / FILE, data)
    except planner.PlanError as exc:
        raise FocusError(str(exc)) from None


@contextmanager
def _locked(root):
    """One writer at a time, also across processes (console and shortcut copies)."""
    with _lock:
        with local_lock.directory_lock(Path(root) / FILE, wait=True):
            yield


# ---------------------------------------------------------------- validation

def _text(value, limit, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise FocusError(f"{what}必须是文字。")
    value = " ".join(value.split())
    if len(value) > limit:
        raise FocusError(f"{what}太长（最多 {limit} 个字）。")
    return value or None


def clean_task(task) -> dict:
    """{course_key, course, title, plan_date, plan_task}; every part optional."""
    if task is None:
        return {}
    if not isinstance(task, dict):
        raise FocusError("任务格式不对。")
    clean = {"course": _text(task.get("course"), 80, "课程名"), "title": _text(task.get("title"), 120, "任务名")}
    key = task.get("course_key")
    if key not in (None, ""):
        if not isinstance(key, str) or not _KEY.match(key):
            raise FocusError("课程编号无效。")
        clean["course_key"] = key
    if task.get("plan_date") or task.get("plan_task"):
        clean["plan_date"] = _day(task.get("plan_date"), "计划日期").isoformat()
        if not isinstance(task.get("plan_task"), str) or not planner._TASK_ID.match(task["plan_task"]):
            raise FocusError("计划任务编号无效。")
        clean["plan_task"] = task["plan_task"]
    return {k: v for k, v in clean.items() if v is not None}


def _valid_session(session):
    """A clean copy of a stored or imported record, or None when it is not one."""
    try:
        if not isinstance(session, dict) or not _ID.match(session.get("id", "")) or session.get("kind") not in KINDS:
            return None
        segments = [(_parse(a), _parse(b)) for a, b in session["segments"]]
        if not segments or any(b < a for a, b in segments) or any(
                segments[i][1] > segments[i + 1][0] for i in range(len(segments) - 1)):
            return None
        seconds = round(sum((b - a).total_seconds() for a, b in segments))
        planned = session.get("planned_seconds")
        if seconds > 86400 or (planned is not None and (type(planned) is not int or planned <= 0)):
            return None
        reason = session.get("reason")
        if reason is not None and reason not in REASONS:
            return None
        clean = {"id": session["id"], "kind": session["kind"], "start": _iso(segments[0][0]),
                 "end": _iso(segments[-1][1]), "segments": [[_iso(a), _iso(b)] for a, b in segments],
                 "focus_seconds": seconds, "planned_seconds": planned,
                 "completed": session.get("completed") is True and session["kind"] != "manual",
                 "task": clean_task(session.get("task"))}
        note = _text(session.get("note"), 500, "备注")
        if reason:
            clean["reason"] = reason
        if note:
            clean["note"] = note
        if session.get("auto_stopped") is True:
            clean["auto_stopped"] = True
        return clean
    except (KeyError, TypeError, ValueError, FocusError):
        return None


def _valid_active(active) -> bool:
    try:
        planned = active["planned_seconds"]
        running = active["running_since"]
        [(_parse(a), _parse(b)) for a, b in active["segments"]]
        return (active["mode"] in MODES and isinstance(active["id"], str) and isinstance(active.get("task", {}), dict)
                and (planned is None or (type(planned) is int and planned > 0))
                and (running is None or bool(_parse(running))) and bool(_parse(active["started_at"])))
    except (KeyError, TypeError, ValueError):
        return False


def _sessions(data) -> list[dict]:
    return [s for s in map(_valid_session, data["sessions"]) if s]


# ---------------------------------------------------------------- the running timer

def _closed(active) -> float:
    return sum((_parse(b) - _parse(a)).total_seconds() for a, b in active["segments"])


def _elapsed(active, now) -> float:
    running = active.get("running_since")
    return _closed(active) + (max(0.0, (now - _parse(running)).total_seconds()) if running else 0.0)


def _close(active, end):
    active["segments"].append([active["running_since"], _iso(end)])
    active["running_since"] = None


def _new_active(mode, planned, now, task) -> dict:
    return {"id": uuid4().hex[:16], "mode": mode, "planned_seconds": planned, "started_at": _iso(now),
            "segments": [], "running_since": _iso(now), "task": task}


def _record(data, active, end, tz, *, completed, reason=None, auto_stopped=False) -> dict:
    session = {"id": active["id"], "kind": "pomodoro" if active["mode"] == "focus" else "stopwatch",
               "start": active["segments"][0][0], "end": _iso(end), "segments": active["segments"],
               "focus_seconds": round(_closed(active)), "planned_seconds": active["planned_seconds"],
               "completed": completed, "task": active.get("task") or {}}
    if reason:
        session["reason"] = reason
    if auto_stopped:
        session["auto_stopped"] = True
    data["sessions"].append(session)
    if completed and session["kind"] == "pomodoro":
        today = _local(end, tz).date().isoformat()
        cycle = data["cycle"]
        data["cycle"] = {"date": today, "count": (cycle.get("count", 0) if cycle.get("date") == today else 0) + 1}
    return session


def _next_break(data) -> str:
    return "long" if data["cycle"]["count"] % data["settings"]["long_every"] == 0 else "short"


def _settle(data, now, tz) -> list[dict]:
    """Finish a running timer whose time is up, at the moment it really ended."""
    active = data["active"]
    if not active or not active.get("running_since"):
        return []
    limit = active["planned_seconds"] or STOPWATCH_LIMIT
    if _elapsed(active, now) < limit:
        return []
    end = _parse(active["running_since"]) + timedelta(seconds=limit - _closed(active))
    _close(active, end)
    data["active"] = None
    if active["mode"] in BREAKS:
        data["next_mode"] = "focus"
        return [{"type": "break_done", "mode": active["mode"], "end": _iso(end)}]
    stopwatch = active["mode"] == "stopwatch"
    session = _record(data, active, end, tz, completed=True, auto_stopped=stopwatch)
    events = [{"type": "focus_done", "session": session}]
    data["next_mode"] = "short" if stopwatch else _next_break(data)
    if not stopwatch and data["settings"]["auto_break"]:
        brk = data["next_mode"]
        data["active"] = _new_active(brk, data["settings"][brk + "_minutes"] * 60, end, active.get("task") or {})
        data["next_mode"] = "focus"
        events += _settle(data, now, tz)  # the break may be over too
    return events


def _today(data, now, tz) -> dict:
    today = _local(now, tz).date()
    seconds, pomodoros, records = 0.0, 0, []
    for session in _sessions(data):
        part = sum(s for start, s in _pieces(session, tz) if start.date() == today)
        if part:
            seconds += part
            records.append(_summary(session))
        if session["completed"] and session["kind"] == "pomodoro" and _local(_parse(session["end"]), tz).date() == today:
            pomodoros += 1
    records.sort(key=lambda s: s["start"], reverse=True)
    cycle = data["cycle"]
    return {"date": today.isoformat(), "focus_seconds": round(seconds), "pomodoros": pomodoros,
            "cycle": cycle.get("count", 0) if cycle.get("date") == today.isoformat() else 0, "records": records}


def _view(data, now, tz, events=()) -> dict:
    active = data["active"]
    if active:
        elapsed = _elapsed(active, now)
        active = {**active, "elapsed": round(elapsed, 1), "paused": not active.get("running_since"),
                  "remaining": None if active["planned_seconds"] is None else round(active["planned_seconds"] - elapsed, 1)}
    return {"now": _iso(now), "settings": data["settings"], "active": active, "next_mode": data["next_mode"],
            "last_task": data["last_task"], "today": _today(data, now, tz), "events": list(events)}


def _change(root, work, now, tz):
    now = _utc(now)
    with _locked(root):
        data = load(root)
        events = _settle(data, now, tz)
        extra = work(data, now) or {}
        _save(root, data)
        return {**_view(data, now, tz, events), **extra}


# ---------------------------------------------------------------- actions (all return the timer view)

def state(root, *, now=None, tz=None) -> dict:
    """The timer as it is now; writes only when a timer ran out since the last look."""
    now = _utc(now)
    with _locked(root):
        data = load(root)
        events = _settle(data, now, tz)
        if events:
            _save(root, data)
        return _view(data, now, tz, events)


def start(root, mode, task=None, minutes=None, *, now=None, tz=None) -> dict:
    if mode not in MODES:
        raise FocusError("模式只能是专注、正计时、短休息或长休息。")
    task = clean_task(task)
    if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= 180):
        raise FocusError("时长必须是 1 到 180 之间的整数分钟。")

    def work(data, now):
        if data["active"]:
            raise FocusError("已经有一个计时在进行，请先结束它。")
        planned = None if mode == "stopwatch" else (minutes or data["settings"][mode + "_minutes"]) * 60
        data["active"] = _new_active(mode, planned, now, task)
        if mode in ("focus", "stopwatch"):
            data["last_task"] = task
    return _change(root, work, now, tz)


def _need_active(data):
    if not data["active"]:
        raise FocusError("现在没有进行中的计时（可能刚好结束了）。")
    return data["active"]


def pause(root, *, now=None, tz=None) -> dict:
    def work(data, now):
        active = _need_active(data)
        if active.get("running_since"):
            _close(active, now)
    return _change(root, work, now, tz)


def resume(root, *, now=None, tz=None) -> dict:
    def work(data, now):
        active = _need_active(data)
        if not active.get("running_since"):
            active["running_since"] = _iso(now)
    return _change(root, work, now, tz)


def stop(root, reason=None, *, save=True, now=None, tz=None) -> dict:
    """End the timer now. Focus time is kept (save=False throws it away); breaks just end."""
    if reason is not None and reason not in REASONS:
        raise FocusError("中断原因无效。")

    def work(data, now):
        active = _need_active(data)
        if active.get("running_since"):
            _close(active, now)
        data["active"] = None
        if active["mode"] in BREAKS:
            data["next_mode"] = "focus"
            return {"saved": None}
        stopwatch = active["mode"] == "stopwatch"
        data["next_mode"] = "short" if stopwatch else "focus"
        if not save or _closed(active) < MIN_SAVED:
            return {"saved": None}
        session = _record(data, active, now, tz, completed=stopwatch, reason=None if stopwatch else reason)
        return {"saved": _summary(session)}
    return _change(root, work, now, tz)


def set_task(root, task, *, now=None, tz=None) -> dict:
    task = clean_task(task)

    def work(data, now):
        data["last_task"] = task
        if data["active"]:
            data["active"]["task"] = task
    return _change(root, work, now, tz)


def update_settings(root, changes, *, now=None, tz=None) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise FocusError("没有要修改的设置。")
    for key, value in changes.items():
        if key not in DEFAULT_SETTINGS:
            raise FocusError(f"未知设置：{key}")
        if isinstance(DEFAULT_SETTINGS[key], bool):
            if type(value) is not bool:
                raise FocusError("开关设置只能是 true 或 false。")
        else:
            low, high = INT_LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise FocusError(f"{key} 必须是 {low} 到 {high} 之间的整数。")

    def work(data, now):
        data["settings"].update(changes)
    return _change(root, work, now, tz)


# ---------------------------------------------------------------- records

def _summary(session) -> dict:
    keys = ("id", "kind", "start", "end", "focus_seconds", "planned_seconds", "completed", "task", "reason", "note",
            "auto_stopped")
    return {k: session[k] for k in keys if k in session}


def _overlaps(sessions, start, end, skip=None) -> bool:
    for session in sessions:
        if session["id"] == skip:
            continue
        for a, b in session["segments"]:
            if _parse(a) < end and start < _parse(b):
                return True
    return False


def add_manual(root, start_local, minutes, task=None, note=None, *, now=None, tz=None) -> dict:
    """Time the user forgot to track: start as local "YYYY-MM-DDTHH:MM", never overlapping a record."""
    try:
        start = datetime.fromisoformat(start_local)
    except (TypeError, ValueError):
        raise FocusError("开始时间格式应为 YYYY-MM-DDTHH:MM。") from None
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz) if tz else start.astimezone()
    start = start.astimezone(timezone.utc)
    if type(minutes) is not int or not 1 <= minutes <= 720:
        raise FocusError("时长必须是 1 到 720 之间的整数分钟。")
    end = start + timedelta(minutes=minutes)
    task, note = clean_task(task), _text(note, 500, "备注")

    def work(data, now):
        if end > now + timedelta(minutes=1):
            raise FocusError("不能补录还没到的时间。")
        if _overlaps(_sessions(data), start, end):
            raise FocusError("这段时间和已有的专注记录重叠了，请换个时间。")
        session = {"id": uuid4().hex[:16], "kind": "manual", "start": _iso(start), "end": _iso(end),
                   "segments": [[_iso(start), _iso(end)]], "focus_seconds": minutes * 60,
                   "planned_seconds": None, "completed": False, "task": task}
        if note:
            session["note"] = note
        data["sessions"].append(session)
        return {"saved": _summary(session)}
    return _change(root, work, now, tz)


def _find(data, session_id):
    for session in data["sessions"]:
        if isinstance(session, dict) and session.get("id") == session_id:
            return session
    raise FocusError("找不到这条记录（可能已经删除）。")


def edit(root, session_id, changes, *, now=None, tz=None) -> dict:
    """Task, note and interruption reason only: recorded times stay what the clock measured."""
    if not isinstance(changes, dict):
        raise FocusError("修改内容格式不对。")
    unknown = set(changes) - {"task", "note", "reason"}
    if unknown:
        raise FocusError("记录的时间不能修改；需要的话请删除后补录。")
    clean = {}
    if "task" in changes:
        clean["task"] = clean_task(changes["task"])
    if "note" in changes:
        clean["note"] = _text(changes["note"], 500, "备注")
    if "reason" in changes:
        if changes["reason"] is not None and changes["reason"] not in REASONS:
            raise FocusError("中断原因无效。")
        clean["reason"] = changes["reason"]

    def work(data, now):
        session = _find(data, session_id)
        if clean.get("reason") and (session.get("kind") != "pomodoro" or session.get("completed")):
            raise FocusError("只有提前结束的番茄钟才有中断原因。")
        for key, value in clean.items():
            if value is None:
                session.pop(key, None)
            else:
                session[key] = value
        return {"saved": _summary(_valid_session(session) or session)}
    return _change(root, work, now, tz)


def delete(root, session_id, *, now=None, tz=None) -> dict:
    def work(data, now):
        data["sessions"].remove(_find(data, session_id))
    return _change(root, work, now, tz)


def export(root) -> dict:
    with _locked(root):
        data = load(root)
    return {"version": 1, "exported_at": _iso(_utc()), "settings": data["settings"], "sessions": _sessions(data)}


def import_records(root, payload, *, now=None, tz=None) -> dict:
    """Merge a backup: new records are added, records already here (same id or overlapping) are skipped."""
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("sessions"), list):
        raise FocusError("这不是小助理导出的专注记录备份。")

    def work(data, now):
        added = skipped = invalid = 0
        known = _sessions(data)
        ids = {s["id"] for s in known}
        for raw in payload["sessions"]:
            session = _valid_session(raw)
            if session is None:
                invalid += 1
            elif session["id"] in ids or _overlaps(known, _parse(session["start"]), _parse(session["end"])):
                skipped += 1
            else:
                data["sessions"].append(session)
                known.append(session)
                ids.add(session["id"])
                added += 1
        return {"imported": {"added": added, "skipped": skipped, "invalid": invalid}}
    return _change(root, work, now, tz)


# ---------------------------------------------------------------- statistics

def _pieces(session, tz):
    """(local start, seconds) per clock hour the session's focus really fell in."""
    for a, b in session["segments"]:
        current, end = _parse(a), _parse(b)
        while current < end:
            boundary = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            step = min(boundary, end)
            yield _local(current, tz), (step - current).total_seconds()
            current = step


def _range(period, anchor: Date, start, end):
    if period == "day":
        return anchor, anchor
    if period == "week":
        low = anchor - timedelta(days=anchor.weekday())
        return low, low + timedelta(days=6)
    if period == "month":
        low = anchor.replace(day=1)
        return low, (low + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if period == "year":
        return anchor.replace(month=1, day=1), anchor.replace(month=12, day=31)
    low, high = _day(start, "开始日期"), _day(end, "结束日期")
    if high < low:
        raise FocusError("结束日期不能早于开始日期。")
    if (high - low).days > 731:
        raise FocusError("自定义范围最多两年。")
    return low, high


def _previous(period, low: Date, high: Date):
    if period == "month":
        before = low - timedelta(days=1)
        return before.replace(day=1), before
    if period == "year":
        return low.replace(year=low.year - 1), high.replace(year=high.year - 1)
    span = (high - low).days + 1
    return low - timedelta(days=span), high - timedelta(days=span)


def _streaks(days: set, today: Date):
    best = run = 0
    previous = None
    for day in sorted(days):
        run = run + 1 if previous and (day - previous).days == 1 else 1
        best, previous = max(best, run), day
    current, cursor = 0, today if today in days else today - timedelta(days=1)
    while cursor in days:
        current, cursor = current + 1, cursor - timedelta(days=1)
    return current, best


def compute_stats(data, period="week", anchor=None, start=None, end=None, *, now=None, tz=None) -> dict:
    """Everything the statistics page draws, for one period; a pure function of the records."""
    if period not in PERIODS:
        raise FocusError("统计范围只能是日、周、月、年或自定义。")
    now = _utc(now)
    today = _local(now, tz).date()
    anchor = _day(anchor) if anchor else today
    low, high = _range(period, anchor, start, end)
    prev_low, prev_high = _previous(period, low, high)
    settings = data["settings"]

    daily = defaultdict(float)          # all time, seconds per local day
    hours = [0.0] * 24
    by_course, by_task = {}, {}
    pomodoros_by_day = defaultdict(int)
    sessions = _sessions(data)
    for session in sessions:
        task = session["task"]
        key = task.get("course_key") or "other"
        in_range = 0.0
        for moment, seconds in _pieces(session, tz):
            daily[moment.date()] += seconds
            if low <= moment.date() <= high:
                hours[moment.hour] += seconds
                in_range += seconds
        if session["completed"] and session["kind"] == "pomodoro":
            pomodoros_by_day[_local(_parse(session["end"]), tz).date()] += 1
        if in_range:
            course = by_course.setdefault(key, {"course_key": key, "course": task.get("course"), "seconds": 0.0})
            course["course"] = course["course"] or task.get("course")
            course["seconds"] += in_range
            title = task.get("title") or None
            item = by_task.setdefault((key, title), {"title": title, "course_key": key, "course": task.get("course"),
                                                     "seconds": 0.0, "sessions": 0})
            item["seconds"] += in_range
            item["sessions"] += 1

    def total(a, b):
        return sum(v for d, v in daily.items() if a <= d <= b)

    ended = [s for s in sessions if s["kind"] != "manual"
             and low <= _local(_parse(s["end"]), tz).date() <= high]
    tomatoes = [s for s in ended if s["kind"] == "pomodoro"]
    interrupted = [s for s in tomatoes if not s["completed"]]
    reasons = defaultdict(int)
    for session in interrupted:
        reasons[session.get("reason") or "none"] += 1
    days_in_range = [low + timedelta(days=i) for i in range((high - low).days + 1)]
    active_days = [d for d in days_in_range if daily.get(d, 0) > 0]
    focus = total(low, high)
    elapsed_days = len([d for d in days_in_range if d <= today])
    best_day = max(active_days, key=lambda d: daily[d], default=None)

    if period == "day":
        buckets = [{"key": str(h), "seconds": round(hours[h])} for h in range(24)]
    elif period == "year":
        buckets = []
        for month in range(1, 13):
            days = [d for d in days_in_range if d.month == month]
            buckets.append({"key": f"{low.year}-{month:02d}", "seconds": round(sum(daily.get(d, 0) for d in days)),
                            "pomodoros": sum(pomodoros_by_day.get(d, 0) for d in days)})
    else:
        buckets = [{"key": d.isoformat(), "seconds": round(daily.get(d, 0)), "pomodoros": pomodoros_by_day.get(d, 0)}
                   for d in days_in_range]

    goal_days = {d for d, v in daily.items()
                 if v >= settings["goal_minutes"] * 60 or pomodoros_by_day.get(d, 0) >= settings["goal_pomodoros"]}
    focus_days = {d for d, v in daily.items() if v > 0}
    streak, best_streak = _streaks(focus_days, today)
    goal_streak, best_goal_streak = _streaks(goal_days, today)
    all_total = sum(daily.values())
    heat_start = today - timedelta(days=364)
    heat_start -= timedelta(days=heat_start.weekday())
    round_items = lambda items: [{**i, "seconds": round(i["seconds"])} for i in items]

    return {
        "period": period, "anchor": anchor.isoformat(), "start": low.isoformat(), "end": high.isoformat(),
        "today": today.isoformat(), "goal": {k: settings[k] for k in ("goal_minutes", "goal_pomodoros")},
        "kpis": {
            "focus_seconds": round(focus), "previous_seconds": round(total(prev_low, prev_high)),
            "pomodoros": len(tomatoes) - len(interrupted), "sessions": len(ended), "interrupted": len(interrupted),
            "completion_rate": round((len(tomatoes) - len(interrupted)) / len(tomatoes), 3) if tomatoes else None,
            "active_days": len(active_days), "days": len(days_in_range),
            "avg_active_day": round(focus / len(active_days)) if active_days else 0,
            "avg_calendar_day": round(focus / elapsed_days) if elapsed_days else 0,
            "longest_seconds": max((s["focus_seconds"] for s in sessions
                                    if low <= _local(_parse(s["end"]), tz).date() <= high), default=0),
            "best_day": best_day.isoformat() if best_day else None,
            "best_day_seconds": round(daily[best_day]) if best_day else 0,
        },
        "buckets": buckets,
        "hours": [round(v) for v in hours],
        "by_course": round_items(sorted(by_course.values(), key=lambda c: -c["seconds"])),
        "by_task": round_items(sorted(by_task.values(), key=lambda t: -t["seconds"])[:10]),
        "reasons": dict(reasons),
        "records": ([_summary(s) for s in sorted(sessions, key=lambda s: s["start"], reverse=True)
                     if any(m.date() == low for m, _ in _pieces(s, tz))] if period == "day" else None),
        "overall": {
            "focus_seconds": round(all_total), "pomodoros": sum(pomodoros_by_day.values()),
            "active_days": len(focus_days), "avg_active_day": round(all_total / len(focus_days)) if focus_days else 0,
            "first_day": min(focus_days).isoformat() if focus_days else None,
            "streak": streak, "best_streak": best_streak,
            "goal_streak": goal_streak, "best_goal_streak": best_goal_streak,
        },
        "heatmap": {"start": heat_start.isoformat(),
                    "days": [round(daily.get(heat_start + timedelta(days=i), 0))
                             for i in range((today - heat_start).days + 1)]},
    }


def stats(root, period="week", anchor=None, start=None, end=None, *, now=None, tz=None) -> dict:
    now = _utc(now)
    with _locked(root):
        data = load(root)
        if _settle(data, now, tz):
            _save(root, data)
    return compute_stats(data, period, anchor, start, end, now=now, tz=tz)


def history(root, start=None, end=None, course=None, query=None, *, now=None, tz=None) -> dict:
    """Records whose focus started in [start, end] (default: the last 30 days), newest first."""
    now = _utc(now)
    today = _local(now, tz).date()
    high = _day(end, "结束日期") if end else today
    low = _day(start, "开始日期") if start else high - timedelta(days=29)
    if high < low:
        raise FocusError("结束日期不能早于开始日期。")
    if course is not None and not _KEY.match(course):
        raise FocusError("课程编号无效。")
    words = (query or "").strip().lower()
    with _locked(root):
        data = load(root)
        if _settle(data, now, tz):
            _save(root, data)
    found = []
    for session in _sessions(data):
        if not low <= _local(_parse(session["start"]), tz).date() <= high:
            continue
        task = session["task"]
        if course and (task.get("course_key") or "other") != course:
            continue
        if words and words not in " ".join(filter(None, (task.get("title"), task.get("course"),
                                                         session.get("note")))).lower():
            continue
        found.append(_summary(session))
    found.sort(key=lambda s: s["start"], reverse=True)
    return {"start": low.isoformat(), "end": high.isoformat(), "records": found[:500],
            "truncated": len(found) > 500, "total_seconds": sum(s["focus_seconds"] for s in found)}


def plan_seconds(root, day: str) -> dict:
    """Focus seconds per task id of one day's plan, for the plan page; never writes."""
    try:
        data = load(root)
    except FocusError:
        return {}
    totals = defaultdict(int)
    for session in _sessions(data):
        task = session["task"]
        if task.get("plan_date") == day and task.get("plan_task"):
            totals[task["plan_task"]] += session["focus_seconds"]
    return dict(totals)
