"""Recoverable, local-only Entrepreneurship migration. No site or credential access."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
from datetime import datetime
from uuid import uuid4

import moodle_sync as sync
import study_materials as materials
from course_map import DEFAULT_MAP, load_course_map
from local_lock import directory_lock

MARKER = ".entrepreneurship-reorganize.json"
PREFIX = "Entrepreneurship_"


def _bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _atomic(path, payload):
    path = materials._ordinary(path)
    temporary = path.with_name(path.name + "." + uuid4().hex[:8] + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _activities(planner_root, course):
    for path in sorted((planner_root / "检查").glob(f"*/moodle-{course}-activities.json"), reverse=True):
        materials._ordinary(path)
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise materials.MaterialError("课程活动快照格式不认识。")
        return {row["id"]: row for row in rows if row.get("course_id") == course}
    raise materials.MaterialError("缺少课程活动快照；请先运行课程检查，再预览整理。")


def _preview(root, official):
    if any(official.glob("*.journal.json")):
        raise materials.MaterialError("存在未完成的同步，请先恢复同步再整理。")
    before, after, operations = {}, {}, []
    index = materials._read(root)
    index_after = deepcopy(index)
    occupied = {}
    old_paths = set()
    states = []
    for course in sync.entrepreneurship.COURSES:
        name = f".moodle-sync-{course}.json"
        state, _ = sync._read_state(official, set(), state_name=name)
        if state["course_id"] != course:
            raise materials.MaterialError("缺少已确认课程编号的同步记录，未整理。")
        before[name] = (official / name).read_bytes()
        state["version"] = 3
        activities = _activities(root, course)
        states.append((name, state, activities))
        old_paths.update(row["local_path"] for row in state["files"].values())
    for name, state, activities in states:
        reading_owners = {}
        for identity, row in sorted(state["files"].items(), key=lambda pair: (pair[1]["cmid"], pair[0])):
            old = row["local_path"]
            source = sync._target(official, old)
            if not source.is_file() or (row["sha256"] is not None and not sync._matches(source, row)):
                raise materials.MaterialError("本地文件缺失或已修改，未开始整理。")
            row.update(sync._fingerprint(source))
            activity = activities.get(row["cmid"])
            if activity is None:
                raise materials.MaterialError("活动快照不含现有资料，未猜测课次。")
            folder = row["folder_path"].split("/") if row["folder_path"] not in ("", "@text") else []
            desired = (sync._layout(official, {"kind": "Other"}, [], row["filename"], True)
                       if row["folder_path"] == "@text" else
                       sync._layout(official, activity, folder, row["filename"], True))
            group = (row["filename"], row["sha256"], row["size"])
            owner = reading_owners.get(group) if row["cmid"] in sync.entrepreneurship.READINGS else None
            if owner is not None:
                desired = state["files"][owner]["local_path"]
                row["alias_of"] = owner
            else:
                target = sync._target(official, desired)
                # Only adopt an existing target when it is already managed and byte-identical.
                if (desired.casefold() in occupied or
                        (sync._exists(target) and (desired not in old_paths or not sync._matches(target, row)))):
                    desired = sync._unique(official, desired, occupied, identity)
                row.pop("alias_of", None)
                occupied[desired.casefold()] = identity
                if row["cmid"] in sync.entrepreneurship.READINGS:
                    reading_owners[group] = identity
            row["local_path"] = desired
            operations.append({"course_id": row["course_id"], "identity": identity,
                               "old": old, "new": desired, "sha256": row["sha256"], "size": row["size"],
                               "alias_of": row.get("alias_of")})
            for item in index_after["items"].values():
                if (item["source"] == "moodle" and item["course_id"] == row["course_id"]
                        and item["identity"] == identity and item["version"] == row["sha256"]):
                    item.update(path=str(sync._target(official, desired)), filename=Path(desired).name)
        sync._normalize_aliases(state)
        sync._validate_aliases(state)
        after[name] = _bytes(state)
    before[materials.INDEX] = (root / materials.INDEX).read_bytes() if (root / materials.INDEX).exists() else None
    after[materials.INDEX] = _bytes(index_after)
    changed = any(row["old"] != row["new"] for row in operations) or any(
        json.loads(before[name]) != json.loads(after[name]) for name in after if before[name] is not None)
    return before, after, operations, changed


def _load_marker(official):
    marker_path = sync._target(official, MARKER)
    value = json.loads(marker_path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"version", "planner", "backup", "operations", "metadata"}
            or value["version"] != 1 or not isinstance(value["operations"], list)
            or not isinstance(value["metadata"], dict)):
        raise materials.MaterialError("整理恢复记录格式不认识，已停止。")
    root = materials._ordinary(value["planner"])
    backup = materials._ordinary(value["backup"])
    if (backup.parent != root / "备份" or not backup.name.startswith(PREFIX)
            or not backup.is_dir()):
        raise materials.MaterialError("整理备份位置无效，已停止。")
    expected = {materials.INDEX, ".moodle-sync-122369.json", ".moodle-sync-122367.json"}
    if set(value["metadata"]) != expected:
        raise materials.MaterialError("整理记录包含未允许的文件，已停止。")
    for row in value["operations"]:
        if (set(row) != {"course_id", "identity", "old", "new", "sha256", "size", "alias_of"}
                or row["course_id"] not in sync.entrepreneurship.COURSES
                or not sync._HASH.fullmatch(str(row["sha256"])) or type(row["size"]) is not int or row["size"] < 0):
            raise materials.MaterialError("整理文件记录无效，已停止。")
        for key in ("old", "new"):
            path = sync._target(official, row[key])
            if Path(row[key]).parts[0] not in sync.KIND_FOLDERS:
                raise materials.MaterialError("整理文件不在允许的分类目录内。")
        if not sync._matches(sync._target(backup / "Official", row["old"]), row):
            raise materials.MaterialError("整理备份内容不符，已停止。")
    for name, signatures in value["metadata"].items():
        if not isinstance(signatures, dict) or set(signatures) != {"before", "after"}:
            raise materials.MaterialError("整理记录的摘要无效。")
        for key in ("before", "after"):
            signature = signatures[key]
            if signature is not None and not sync._HASH.fullmatch(str(signature)):
                raise materials.MaterialError("整理记录的摘要无效。")
        payload = materials._ordinary(backup / "after" / name).read_bytes()
        if _hash(payload) != signatures["after"]:
            raise materials.MaterialError("整理后的记录备份已变化，已停止。")
        original = backup / name if name == materials.INDEX else backup / "Official" / name
        materials._ordinary(original)
        if (_hash(original.read_bytes()) if original.exists() else None) != signatures["before"]:
            raise materials.MaterialError("整理前的记录备份已变化，已停止。")
        # Validate the destination data before replacing metadata on recovery.
        if name != materials.INDEX:
            sync._read_state(backup / "after", set(), state_name=name)
        else:
            materials._read(backup / "after")
    return value, root, backup


def _finish(official):
    value, root, backup = _load_marker(official)
    targets = {row["new"] for row in value["operations"]}
    # Recheck all current inputs before making any recovery changes.
    for name, signatures in value["metadata"].items():
        target = root / name if name == materials.INDEX else official / name
        materials._ordinary(target)
        current = _hash(target.read_bytes()) if target.exists() else None
        if current not in (signatures["before"], signatures["after"]):
            raise materials.MaterialError("整理期间记录被其他进程修改，已停止恢复。")
    for row in value["operations"]:
        if not sync._target(official, row["old"]).exists() and not sync._target(official, row["new"]).exists():
            raise materials.MaterialError("整理期间原文件已丢失，已停止恢复。")
        for relative in {row["old"], row["new"]}:
            path = sync._target(official, relative)
            if path.exists() and not sync._matches(path, row):
                raise materials.MaterialError("整理期间文件已变化，已保留并停止。")
    for row in value["operations"]:
        target = sync._target(official, row["new"])
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + "." + uuid4().hex[:8] + ".part")
            shutil.copy2(sync._target(backup / "Official", row["old"]), temporary)
            if not sync._matches(temporary, row):
                raise materials.MaterialError("整理复制校验失败，已停止。")
            sync._move_no_replace(temporary, target)
    for name in value["metadata"]:
        target = root / name if name == materials.INDEX else official / name
        _atomic(target, (backup / "after" / name).read_bytes())
    retired = set()
    for row in value["operations"]:
        if row["old"] in targets or row["old"] in retired:
            continue
        retired.add(row["old"])
        old = sync._target(official, row["old"])
        if old.exists():
            destination = sync._target(backup / "retired", row["old"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            sync._move_no_replace(old, destination)
    for row in value["operations"]:
        if not sync._matches(sync._target(official, row["new"]), row):
            raise materials.MaterialError("整理最终校验未通过，已保留恢复记录。")
    receipt = {"ok": True, "backup": str(backup), "sources": len(value["operations"]),
               "files": len(targets), "operations": value["operations"]}
    _atomic(backup / "receipt.json", _bytes(receipt))
    sync._target(official, MARKER).unlink()
    return receipt


def recover(official):
    """Caller holds Official's directory lock; lock order is Official then Planner."""
    value, root, _ = _load_marker(Path(official))
    with directory_lock(root):
        return _finish(Path(official))


def reorganize(root, *, course="MGTHN0133", apply=False, map_path=DEFAULT_MAP):
    root = materials._ordinary(Path(root).absolute())
    if course != "MGTHN0133":
        raise materials.MaterialError("目前只允许整理 Entrepreneurship。")
    entries = [e for e in load_course_map(map_path) if e.folder.startswith(course + " ")]
    if {e.moodle_id for e in entries} != set(sync.entrepreneurship.COURSES):
        raise materials.MaterialError("课程对照表与已确认的两门课不一致。")
    destinations = {e.moodle_dest for e in entries}
    if len(destinations) != 1:
        raise materials.MaterialError("两门课必须共用同一个 Official。")
    official = materials._ordinary(next(iter(destinations)))
    with directory_lock(official):
        if sync._target(official, MARKER).exists():
            if not apply:
                raise materials.MaterialError("上次整理尚未完成，请使用 --apply 恢复。")
            return recover(official)
        with directory_lock(root):
            before, after, operations, changed = _preview(root, official)
            result = {"dry_run": not apply, "changed": changed, "course": course,
                      "operations": operations, "files": len({r["new"] for r in operations})}
            if not apply or not changed:
                return result
            backup = root / "备份" / (PREFIX + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8])
            backup.mkdir(parents=True, exist_ok=False)
            # Reject links and snapshot the entire directory, including both state files.
            for path in official.rglob("*"):
                materials._ordinary(path)
            shutil.copytree(official, backup / "Official")
            (backup / "after").mkdir()
            for name, payload in before.items():
                if name == materials.INDEX and payload is not None:
                    (backup / name).write_bytes(payload)
            for name, payload in after.items():
                (backup / "after" / name).write_bytes(payload)
            # A concurrent external edit during the backup must never be adopted.
            for name, payload in before.items():
                current = root / name if name == materials.INDEX else official / name
                if (current.read_bytes() if current.exists() else None) != payload:
                    raise materials.MaterialError("备份期间记录发生变化，未开始整理。")
            marker = {"version": 1, "planner": str(root), "backup": str(backup), "operations": operations,
                      "metadata": {name: {"before": _hash(before[name]) if before[name] is not None else None,
                                          "after": _hash(payload)} for name, payload in after.items()}}
            _atomic(sync._target(official, MARKER), _bytes(marker))
            return _finish(official)
