"""Persistent, local-only handoff from downloads to reading and published plans.

Sync reports are data, never instructions. No credentials, HTTP or PDF interpreter
are involved here. Versions are content hashes; paths may move to an _old file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4
from urllib.parse import parse_qsl, unquote, urlsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_lock import directory_lock

INDEX = "材料索引.json"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LEAK = re.compile(r'''(?:sesskey|MoodleSession\w*|jwt|access_token|password|SAMLResponse)["']?\s*[=:]\s*["']?[^\s,;]+''', re.I)
_AUTH = re.compile(r"sesskey|cookie|token|password|secret|samlresponse|jwt", re.I)


class MaterialError(ValueError):
    pass


def _leaks(value):
    for _ in range(3):
        if _LEAK.search(value):
            return True
        value = unquote(value)
    return False


def _text(value, *, maximum=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or _leaks(value):
        raise MaterialError("材料记录文字无效或含登录信息。")
    return value.strip()


def _url(value, source):
    host = "www.moodle.tum.de" if source == "moodle" else "artemis.tum.de"
    try:
        p = urlsplit(value)
        valid = (p.scheme == "https" and p.hostname == host and p.port in (None, 443)
                 and not p.username and not p.password and not p.fragment
                 and not any(_AUTH.search(unquote(unquote(k))) for k, _ in parse_qsl(p.query)))
    except (ValueError, TypeError):
        valid = False
    if not valid or _leaks(value):
        raise MaterialError("材料来源链接不符合站点限制。")
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(root):
    path = Path(root) / INDEX
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "items": {}, "sources": {}}
    except (ValueError, UnicodeError, OSError):
        raise MaterialError("材料索引损坏，已停止写入。") from None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("items"), dict):
        raise MaterialError("材料索引格式不认识，已停止写入。")
    if not isinstance(value.get("sources", {}), dict):
        raise MaterialError("材料来源状态格式不认识。")
    for key, item in value["items"].items():
        if (not isinstance(item, dict) or item.get("id") != key or not _HASH.fullmatch(str(item.get("sha256", "")))
                or item.get("version") != item.get("sha256")
                or item.get("source") not in ("moodle", "artemis")
                or type(item.get("course_id")) is not int or item["course_id"] <= 0
                or not isinstance(item.get("course_key"), str)
                or not isinstance(item.get("identity"), str) or not item["identity"]
                or not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute()
                or not isinstance(item.get("filename"), str)
                or type(item.get("size")) is not int or item["size"] < 0
                or not isinstance(item.get("first_seen"), str)
                or not isinstance(item.get("summaries"), list)
                or any(not isinstance(s, dict) or not isinstance(s.get("text"), str) for s in item.get("summaries", []))
                or not isinstance(item.get("read_ranges"), list)
                or any(not isinstance(r, list) or len(r) != 2 or any(type(n) is not int or n < 1 for n in r)
                       or r[1] < r[0] for r in item.get("read_ranges", []))
                or not isinstance(item.get("planned_dates"), list)
                or item.get("status") not in ("pending", "summarized", "planned", "skipped")):
            raise MaterialError("材料索引包含无效条目，已停止写入。")
        _url(item.get("url"), item["source"])
    if _leaks(json.dumps(value, ensure_ascii=False)):
        raise MaterialError("材料索引包含登录信息，已停止读取。")
    return value


def _write(root, data):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / INDEX
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ordinary(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise MaterialError("材料路径不能经过符号链接或联接。")
    return path


def _relative_file(dest, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise MaterialError("材料相对路径无效。")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not posix.parts or any(p in ("..", ".") for p in posix.parts):
        raise MaterialError("材料路径越界。")
    base = _ordinary(dest).resolve()
    path = _ordinary(base.joinpath(*posix.parts))
    if not path.resolve().is_relative_to(base) or not path.is_file():
        raise MaterialError("材料文件不存在或路径越界。")
    return path


def _available(item):
    try:
        path = _ordinary(item["path"])
        return path.is_file() and path.stat().st_size == item["size"] and digest(path) == item["sha256"]
    except (OSError, KeyError, MaterialError):
        return False


def ingest(root, report, dest_dir, source, course_key):
    """Import *all* valid artifacts, including unchanged files; never consume them."""
    if source not in ("moodle", "artemis") or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", course_key or ""):
        raise MaterialError("材料来源或课程代号无效。")
    if not isinstance(report, dict) or report.get("dry_run") is not False or not isinstance(report.get("artifacts"), list):
        raise MaterialError("需要真实同步完成后的材料清单，预览不能推进材料状态。")
    accepted = []
    for art in report["artifacts"]:
        if not isinstance(art, dict):
            raise MaterialError("同步材料条目无效。")
        identity = _text(art.get("identity"), maximum=4096)
        url = _url(art.get("url"), source)
        course_id = art.get("course_id")
        if type(course_id) is not int or course_id <= 0:
            raise MaterialError("材料课程编号无效。")
        if not isinstance(art.get("history", []), list) or any(not isinstance(r, dict) for r in art.get("history", [])):
            raise MaterialError("材料历史版本格式无效。")
        for revision in [*art.get("history", []), art]:
            sha = revision.get("sha256")
            size = revision.get("size")
            if not isinstance(sha, str) or not _HASH.fullmatch(sha) or type(size) is not int or size < 0:
                raise MaterialError("材料版本摘要或字节数无效。")
            path = _relative_file(dest_dir, revision.get("local_path"))
            if path.stat().st_size != size or digest(path) != sha:
                raise MaterialError("材料内容与同步记录不符，请重新同步或核对本地修改。")
            key = hashlib.sha256(f"{source}:{course_id}:{identity}:{sha}".encode("utf-8")).hexdigest()
            accepted.append({"id": key, "source": source, "course_id": course_id, "course_key": course_key,
                             "identity": identity, "version": sha, "sha256": sha, "size": size,
                             "path": str(path), "filename": path.name, "url": url,
                             "current": revision is art})
    with directory_lock(root):
        data = _read(root)
        for item in accepted:
            old = data["items"].get(item["id"])
            if old is None:
                old = {"status": "pending" if item["current"] else "skipped", "summaries": [],
                       "read_ranges": [], "page_count": None, "planned_dates": [], "first_seen": _stamp()}
                if not item["current"]:
                    old["skip_reason"] = "初始化时发现的历史版本，保留用于历史计划引用。"
            old.update(item, last_seen=_stamp())
            if not item["current"] and old["status"] in ("pending", "summarized"):
                old.update(status="skipped", skip_reason="已有更新版本，改用新版学习；历史版本仍可追溯。")
            data["items"][item["id"]] = old
        source_key = f"{source}:{report.get('course_id')}"
        errors = report.get("errors") or []
        data.setdefault("sources", {})[source_key] = {
            "source": source, "course_key": course_key, "checked_at": _stamp(),
            "ok": not errors and not report.get("failed", {}).get("count", 0),
            "artifacts": len(report["artifacts"]), "coverage": report.get("coverage", []),
            "failures": len(errors),
        }
        # Coverage can contain server text. Do not persist a credential-shaped value.
        if _leaks(json.dumps(data["sources"][source_key], ensure_ascii=False)):
            raise MaterialError("来源报告包含登录信息，已拒绝保存。")
        _write(root, data)
    return {"ok": True, "imported": len(accepted), "pending": sum(i["status"] == "pending" for i in data["items"].values())}


def _merge_ranges(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def record_summary(root, material_id, summary, *, pages=None, page_count=None, complete=False, skip_reason=None):
    if skip_reason is None:
        summary = _text(summary)
    else:
        skip_reason = _text(skip_reason, maximum=1000)
    if pages is not None and (not isinstance(pages, (list, tuple)) or len(pages) != 2
                             or any(type(n) is not int or n < 1 for n in pages) or pages[1] < pages[0]):
        raise MaterialError("页码范围必须是递增的正整数。")
    if page_count is not None and (type(page_count) is not int or page_count < 1):
        raise MaterialError("总页数无效。")
    with directory_lock(root):
        data = _read(root)
        item = data["items"].get(material_id)
        if item is None or not _available(item):
            raise MaterialError("材料不存在、已移动或内容已改变。")
        item = _shared_view(data, item)
        data["items"][material_id] = item
        if item["status"] == "planned":
            raise MaterialError("已发布计划引用的解读记录不能覆盖。")
        count = page_count if page_count is not None else item.get("page_count")
        if pages and count is not None and pages[1] > count:
            raise MaterialError("页码超过材料总页数。")
        if pages:
            item["read_ranges"] = _merge_ranges([*item.get("read_ranges", []), list(pages)])
        item["page_count"] = count
        if skip_reason:
            item.update(status="skipped", skip_reason=skip_reason)
        else:
            entry = {"text": summary, "pages": list(pages) if pages else None}
            if not any(s.get("text") == summary and s.get("pages") == entry["pages"] for s in item["summaries"]):
                item["summaries"].append({**entry, "at": _stamp()})
            if complete:
                if item["path"].lower().endswith(".pdf") and (not count or item["read_ranges"] != [[1, count]]):
                    raise MaterialError("PDF 尚有未读取页码，不能标记为完整解读。")
                item["status"] = "summarized"
        item["updated_at"] = _stamp()
        if item.get("course_id") in (122369, 122367) and item.get("source") == "moodle":
            for peer in data["items"].values():
                if (peer["source"] == "moodle" and peer["course_id"] == item["course_id"]
                        and peer["version"] == item["version"]
                        and os.path.normcase(peer["path"]) == os.path.normcase(item["path"])):
                    for key in ("read_ranges", "page_count", "summaries", "status", "updated_at", "planned_dates", "skip_reason"):
                        if key in item:
                            peer[key] = item[key]
        _write(root, data)
    return {"ok": True, "id": material_id, "status": item["status"], "read_ranges": item["read_ranges"]}


def resolve(root, material_id, version=None):
    data = _read(root)
    item = data["items"].get(material_id)
    if item is None or (version is not None and version != item["version"]) or not _available(item):
        raise MaterialError("材料版本不存在或本地内容不符。")
    return _shared_view(data, item)


def _shared_view(data, item):
    """Declared shared paths keep source IDs, but share reading progress."""
    result = dict(item)
    if item.get("course_id") not in (122369, 122367) or item.get("source") != "moodle":
        return result
    peers = [row for row in data["items"].values() if row.get("source") == "moodle"
             and row.get("course_id") == item["course_id"] and row["version"] == item["version"]
             and os.path.normcase(row["path"]) == os.path.normcase(item["path"])]
    result["read_ranges"] = _merge_ranges([r for row in peers for r in row["read_ranges"]])
    result["planned_dates"] = sorted({day for row in peers for day in row["planned_dates"]})
    result["summaries"] = []
    for row in peers:
        for summary in row["summaries"]:
            if summary not in result["summaries"]:
                result["summaries"].append(summary)
    counts = {row.get("page_count") for row in peers if row.get("page_count") is not None}
    if len(counts) == 1:
        result["page_count"] = counts.pop()
    statuses = {row["status"] for row in peers}
    if "planned" in statuses:
        result["status"] = "planned"
    elif "summarized" in statuses:
        result["status"] = "summarized"
    elif "pending" in statuses:
        result["status"] = "pending"
    return result


def _queue_items(data):
    seen = set()
    for item in data["items"].values():
        if item.get("course_id") in (122369, 122367) and item.get("source") == "moodle":
            key = (item["course_id"], os.path.normcase(item["path"]), item["version"])
            if key in seen:
                continue
            seen.add(key)
            item = _shared_view(data, item)
        yield item


def _reconcile(data, root):
    """Published plans are authoritative even if the process died before ack."""
    for path in sorted((Path(root) / "计划").glob("????-??-??.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
            continue
        for task in plan["tasks"]:
            if not isinstance(task, dict) or not isinstance(task.get("materials", []), list):
                continue
            for ref in task.get("materials", []):
                if not isinstance(ref, dict):
                    continue
                candidates = [data["items"].get(ref.get("id"))] if ref.get("id") else data["items"].values()
                for item in candidates:
                    if not item or (not ref.get("id") and os.path.normcase(item["path"]) != os.path.normcase(str(ref.get("path")))):
                        continue
                    if ref.get("version") and ref["version"] != item["version"]:
                        continue
                    if item["status"] in ("summarized", "planned"):
                        item["status"] = "planned"
                        item["planned_dates"] = sorted(set([*item.get("planned_dates", []), path.stem]))


def pending(root, limit=100):
    data = _read(root)
    _reconcile(data, root)
    items, unavailable = [], []
    for item in _queue_items(data):
        if item["status"] not in ("pending", "summarized"):
            continue
        if not _available(item):
            unavailable.append({"id": item["id"], "filename": item["filename"], "reason": "文件缺失或内容与版本不符"})
            continue
        items.append(item)
    items.sort(key=lambda x: (x["first_seen"], x["course_key"], x["filename"]))
    return {"items": items[:limit], "total": len(items), "unavailable": unavailable,
            "sources": list(data.get("sources", {}).values()), "page_budget": 150}


def status(root):
    """Cheap dashboard receipt; content verification happens before consumption."""
    data = _read(root)
    _reconcile(data, root)
    todo = [i for i in _queue_items(data) if i["status"] in ("pending", "summarized")]
    unavailable = 0
    for item in todo:
        try:
            path = _ordinary(item["path"])
            unavailable += not path.is_file() or path.stat().st_size != item["size"]
        except (OSError, MaterialError):
            unavailable += 1
    return {"pending": len(todo), "unavailable": unavailable, "sources": list(data.get("sources", {}).values())}


def mark_planned(root, day, refs):
    # Only acknowledge references actually present in a published local plan.
    with directory_lock(root):
        data = _read(root)
        _reconcile(data, root)
        _write(root, data)
    return {"ok": True, "date": day}


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(prog="planner.py materials")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("ingest")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--source", choices=("moodle", "artemis"), required=True)
    p.add_argument("--course", required=True)
    commands.add_parser("pending")
    p = commands.add_parser("reorganize")
    p.add_argument("--course", required=True, choices=("MGTHN0133",))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--map", type=Path)
    p = commands.add_parser("summarize")
    p.add_argument("id")
    p.add_argument("--text", required=True)
    p.add_argument("--pages", nargs=2, type=int)
    p.add_argument("--page-count", type=int)
    p.add_argument("--complete", action="store_true")
    p = commands.add_parser("skip")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    root = Path(root or Path(os.environ.get("TUM_STUDY_ROOT") or Path(__file__).resolve().parent.parent) / "Planner")
    try:
        if args.command == "ingest":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            result = ingest(root, report, args.dest, args.source, args.course)
        elif args.command == "pending":
            result = pending(root)
        elif args.command == "reorganize":
            from material_reorganize import reorganize
            kwargs = {"map_path": args.map} if args.map else {}
            result = reorganize(root, course=args.course, apply=args.apply, **kwargs)
        elif args.command == "skip":
            result = record_summary(root, args.id, "", skip_reason=args.reason)
        else:
            result = record_summary(root, args.id, args.text, pages=args.pages, page_count=args.page_count, complete=args.complete)
    except (MaterialError, ValueError, OSError, RuntimeError) as exc:
        result = {"error": str(exc) if isinstance(exc, MaterialError) else "材料操作失败，已保留现有记录。"}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 1 if "error" in result else 0
