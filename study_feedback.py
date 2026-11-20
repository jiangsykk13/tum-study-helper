"""Read-only recent learning feedback. Suggestions never change time budgets."""
from datetime import date, timedelta
import json
from pathlib import Path
from statistics import median


def summarize(root, day):
    last = date.fromisoformat(day)
    first = (last - timedelta(days=6)).isoformat()
    root = Path(root)
    try:
        progress = json.loads((root / "进度.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        progress = {"version": 1, "days": {}}
    except (ValueError, OSError):
        return {"error": "进度记录无法读取，未推断学习表现。"}
    if not isinstance(progress, dict) or progress.get("version") != 1 or not isinstance(progress.get("days"), dict):
        return {"error": "进度记录格式不认识，未推断学习表现。"}
    groups, review = {}, {}
    for path in sorted((root / "计划").glob("????-??-??.json")):
        if not first <= path.stem <= day:
            continue
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            continue
        entries = progress["days"].get(path.stem, {})
        if not isinstance(entries, dict):
            continue
        for task in plan["tasks"]:
            if not isinstance(task, dict) or task.get("section") == "research":
                continue
            entry = entries.get(task.get("id"), {})
            if not isinstance(entry, dict):
                continue
            minutes, understanding = entry.get("actual_minutes"), entry.get("understanding")
            if minutes is None and understanding is None:
                continue
            key = (task.get("course_key"), task.get("kind"))
            group = groups.setdefault(key, {"course_key": key[0], "kind": key[1], "course": task.get("course"),
                                            "observations": 0, "actual_minutes": 0, "ratios": [], "difficult": 0})
            group["observations"] += 1
            if type(minutes) is int and 1 <= minutes <= 1440:
                group["actual_minutes"] += minutes
                share = {"done": 1, "half": .5}.get(entry.get("state"), 0)
                estimate = task.get("minutes")
                if share and type(estimate) in (int, float) and estimate > 0:
                    group["ratios"].append(minutes / (estimate * share))
            if understanding in ("hard", "not_understood"):
                group["difficult"] += 1
                work_key = task.get("carry_of") or f"{path.stem}/{task['id']}"
                review[work_key] = {"key": work_key, "title": task.get("title"),
                               "course": task.get("course"), "course_key": task.get("course_key"),
                               "understanding": understanding, "suggested_minutes": 10,
                               "note": "在每日预算内安排一次简短回忆或同类自测，不额外堆欠账。"}
            elif understanding == "understood":
                review.pop(task.get("carry_of") or f"{path.stem}/{task['id']}", None)
    rows = []
    for group in groups.values():
        ratios = group.pop("ratios")
        group["timing_samples"] = len(ratios)
        group["suggested_time_factor"] = round(max(.5, min(2, median(ratios))), 2) if len(ratios) >= 3 else None
        rows.append(group)
    return {"from": first, "to": day, "courses": rows, "review_suggestions": list(review.values()),
            "note": "反馈样本少于3次时只展示记录，不推断估时系数；不自动修改课程比例。"}
