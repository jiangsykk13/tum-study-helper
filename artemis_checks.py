"""The user's own ticks for Artemis exercises, kept in the planner folder.

    <planner>/Artemis练习.json = {"version": 1, "checks": {"<course id>:<exercise id>":
                                  {"at": ISO time, "title": exercise title}}}

Only the web page writes it: a tick is the user's own judgement ("I have done this"),
independent of Artemis' score. Unticking removes the entry. Writes are atomic
(planner._write_json) and serialised across processes; a damaged or unknown file is
never overwritten. Nothing here talks to Artemis.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import local_lock
import planner

FILE = "Artemis练习.json"
MAX_TITLE = 200
_lock = threading.Lock()


class CheckError(Exception):
    pass


def load(root) -> dict:
    path = Path(root) / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "checks": {}}
    except (OSError, UnicodeDecodeError, ValueError):
        raise CheckError("Artemis 练习勾选记录无法读取或已损坏，已停止写入以免覆盖。")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("checks"), dict):
        raise CheckError("Artemis 练习勾选记录的格式不认识，已停止写入以免覆盖。")
    return data


def key(course_id, exercise_id) -> str:
    for value in (course_id, exercise_id):
        if type(value) is not int or value <= 0:
            raise CheckError("课程编号和练习编号必须是正整数。")
    return f"{course_id}:{exercise_id}"


def course_checks(root, course_id: int) -> dict:
    """{exercise id: entry} for one course (read only)."""
    prefix = f"{course_id}:"
    checks = load(root)["checks"]
    return {int(k[len(prefix):]): v for k, v in checks.items()
            if k.startswith(prefix) and k[len(prefix):].isdigit() and isinstance(v, dict)}


def set_done(root, course_id, exercise_id, done, title="") -> dict:
    """Tick (done=True) or untick one exercise; returns that course's checks."""
    name = key(course_id, exercise_id)
    if not isinstance(done, bool):
        raise CheckError("done 必须是 true 或 false。")
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        raise CheckError(f"练习标题必须是最多 {MAX_TITLE} 个字的文字。")
    with _lock, local_lock.directory_lock(Path(root) / FILE, wait=True):
        data = load(root)
        if done:
            data["checks"][name] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "title": title.strip()}
        elif data["checks"].pop(name, None) is None:
            return course_checks(root, course_id)
        try:
            planner._write_json(Path(root) / FILE, data)
        except planner.PlanError as exc:
            raise CheckError(str(exc)) from None
    return course_checks(root, course_id)
