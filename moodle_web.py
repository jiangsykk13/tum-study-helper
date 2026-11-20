"""Local web UI: the MCP tools behind a small JSON API on 127.0.0.1, plus static pages in web/.

    python -I tum_moodle_auto.py ui [--port N] [--no-browser] [--map 课程对照表.md] [--planner DIR]
    pythonw -I tum_moodle_auto.py ui --app   # desktop shortcut: a Chrome tab, no console (app_window.py)

Only this computer can reach it. Every /api request needs the per-launch token
(sent by the page as X-UI-Token), the exact 127.0.0.1 Host and no foreign Origin,
so other web sites open in the browser cannot use it. With --tailscale, the user's
other devices may also come in through `tailscale serve` (Host = this computer's
tailnet name, Tailscale-User-Login = the user's own login; see LocalOnly). All tool calls run one at a
time through the same guarded functions as the MCP server (redaction, URL
allow-lists, read-only Moodle/Artemis). Sessions live only in this process.
The study plan page reads the planner folder directly (planner.py) and never logs
in; its ticks are the only thing it writes, into that folder's 进度.json. The
pomodoro timer and its statistics (focus.py) likewise only use 专注记录.json there,
and the Artemis exercise page's own ticks (artemis_checks.py) only Artemis练习.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import NamedTuple

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import artemis_checks
import course_map
import focus
import planner

WEB_DIR = Path(__file__).resolve().parent / "web"
ARTEMIS_PREFIX = "https://artemis.tum.de/"
COURSE_LISTS = ("inprogress", "past", "future", "all")
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "content-security-policy": ("default-src 'self'; img-src 'self' data:; object-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


class BadRequest(Exception):
    pass


class Tailnet(NamedTuple):
    """This computer's names on the user's tailnet and the one login allowed to use them."""
    hosts: frozenset
    login: str


def tailnet_identity(timeout: float = 5) -> Tailnet | None:
    """Ask the local Tailscale client who we are; None when it is missing, stopped or not logged in."""
    exe = shutil.which("tailscale") or os.path.expandvars(r"%ProgramFiles%\Tailscale\tailscale.exe")
    try:
        done = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status = json.loads(done.stdout)
        me = status["Self"]
        name = me["DNSName"].rstrip(".").lower()
        login = status["User"][str(me["UserID"])]["LoginName"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not name or "@" not in login:
        return None
    return Tailnet(frozenset({name, name.split(".")[0]}), login)


class LocalOnly:
    """ASGI guard: exact local Host for everything; token and same origin for /api.

    With a tailnet, `tailscale serve` (http://<this computer>.<tailnet>.ts.net → 127.0.0.1:port)
    may also come in: Tailscale keeps the Host and replaces any Tailscale-User-Login the client
    sent with the real sender (checked 2026-11-17), so that login stands in for the token.
    """

    def __init__(self, app, port: int, token: str, tailnet: Tailnet | None = None):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}
        self.token = token.encode()
        self.tailnet = tailnet
        self.remote_hosts = set()
        if tailnet:
            self.remote_hosts = {f"{host}{suffix}" for host in tailnet.hosts for suffix in ("", ":80", ":443")}
        self.remote_origins = {f"{scheme}://{host}" for scheme in ("http", "https") for host in self.remote_hosts}

    def _remote(self, headers) -> bool:
        """A request that came through `tailscale serve` from the user's own Tailscale account."""
        return (headers.get("host") in self.remote_hosts
                and secrets.compare_digest(headers.get("tailscale-user-login", "").encode(),
                                           self.tailnet.login.encode()))

    def _allowed(self, scope) -> bool:
        headers = Headers(scope=scope)
        remote = bool(self.tailnet) and self._remote(headers)
        if headers.get("host") not in self.hosts and not remote:
            return False
        if not scope["path"].startswith("/api/"):
            return True
        origin = headers.get("origin")
        if origin is not None and origin not in (self.remote_origins if remote else self.origins):
            return False
        if headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return False
        if remote:
            scope["tailnet"] = True  # e.g. no opening folders on this screen for a page on another computer
            return True
        return secrets.compare_digest(headers.get("x-ui-token", "").encode(), self.token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        if not self._allowed(scope):
            response = JSONResponse({"error": "拒绝访问：请用桌面快捷方式（或启动窗口）新打开的页面。"}, status_code=403)
            return await response(scope, receive, send_with_headers)
        await self.app(scope, receive, send_with_headers)


class Api:
    def __init__(self, tools: dict, map_path: Path, opener=None, planner_dir: Path = planner.DEFAULT_DIR):
        self.tools = tools
        self.planner_dir = Path(planner_dir)
        self.map_path = Path(map_path)
        self.root = self.map_path.parent
        self.opener = opener or getattr(os, "startfile", None)
        # One requests session per client and long syncs: never run tools concurrently.
        self.lock = threading.Lock()

    def _call(self, name, **kwargs):
        with self.lock:
            return self.tools[name](**kwargs)

    async def call(self, name, **kwargs):
        return await run_in_threadpool(self._call, name, **kwargs)

    async def planned(self, function, *args, **kwargs):
        """Planner files only (no tools, no login); their problems become readable 400s."""
        def work():
            try:
                return function(self.planner_dir, *args, **kwargs)
            except planner.PlanError as exc:
                raise BadRequest(str(exc))
        return await run_in_threadpool(work)

    def entries(self) -> tuple[list[dict], str | None]:
        try:
            return [e.to_dict() for e in course_map.load_course_map(self.map_path)], None
        except FileNotFoundError:
            return [], f"没有找到课程对照表：{self.map_path}"
        except (OSError, UnicodeDecodeError):
            return [], "课程对照表无法读取。"

    # -- request helpers ---------------------------------------------------

    @staticmethod
    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise BadRequest("请求内容不是有效的 JSON。")
        if not isinstance(data, dict):
            raise BadRequest("请求内容必须是对象。")
        return data

    @staticmethod
    def text(data: dict, key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BadRequest(f"缺少参数 {key}。")
        return value.strip()

    @staticmethod
    def folder(data: dict, key: str = "dest_dir") -> str:
        value = Api.text(data, key)
        if not Path(value).is_absolute():
            raise BadRequest("目标文件夹必须是完整路径，例如 D:\\TUM\\_Inbox。")
        return value

    @staticmethod
    def confirmed(data: dict):
        if data.get("confirm") is not True:
            raise BadRequest("这个操作会写入文件，需要先在界面里确认。")

    # -- endpoints -----------------------------------------------------------

    async def plan(self, request: Request):
        day = request.query_params.get("date") or None
        result = await self.planned(planner.view, day)
        return await run_in_threadpool(self.enrich_plan, result)

    def enrich_plan(self, result):
        """Join latest local practice results; opening the plan never runs code."""
        import json
        by_folder = {}
        try:
            records = json.loads((self.planner_dir / "练习记录.json").read_text(encoding="utf-8"))
            problems = records.get("problems", []) if records.get("version") == 1 else []
            by_folder = {os.path.normcase(str(Path(p["folder"]).resolve())): p for p in problems if isinstance(p, dict) and p.get("folder")}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            pass
        tasks = [*(result.get("plan") or {}).get("tasks", []),
                 *(result.get("previous") or {}).get("tasks", [])]
        for task in tasks:
            try:
                for material in task.get('materials') or []:
                    if isinstance(material, dict):
                        material['unavailable'] = not Path(material.get('path') or '').is_file()
                practice = task.get("practice")
                if not isinstance(practice, dict) or not practice.get("folder"):
                    continue
                problem = by_folder.get(os.path.normcase(str(Path(practice["folder"]).resolve())))
                practice['attempt_state'] = 'unknown'
                practice['folder_missing'] = not Path(practice['folder']).is_dir()
                if problem and isinstance(problem.get("last_result"), dict):
                    last = problem["last_result"]
                    practice.update(tests_passed=last.get("passed"), tests_total=last.get("total"),
                                    checked_at=last.get("checked_at"), error=last.get("error"))
                    baseline, fingerprint = problem.get('skeleton_sha256'), last.get('code_sha256')
                    if baseline and fingerprint:
                        practice['attempt_state'] = 'untouched' if baseline == fingerprint else 'attempted'
                if not problem:
                    practice['record_note'] = '找不到登记记录，尚无可靠检测记录。'
            except (OSError, ValueError, TypeError, RuntimeError):
                task['resource_note'] = '资料或练习路径暂时无法读取。'
        try:
            import study_materials
            result["materials_status"] = study_materials.status(self.planner_dir)
        except (OSError, ValueError):
            result["materials_status"] = {"error": "材料索引暂时无法读取，请核对最近运行记录。"}
        self.add_focus_time(result)
        if result.get('date') and isinstance(result.get('summary'), dict):
            try:
                # stats() settles timers and may write. Opening a plan must only read.
                recorded = focus.compute_stats(focus.load(self.planner_dir), period='day', anchor=result['date'])
                result['summary']['recorded_focus_seconds'] = recorded['kpis']['focus_seconds']
            except (focus.FocusError, OSError, ValueError):
                result['summary']['focus_error'] = '专注记录暂时无法读取。'
        if result.get("date"):
            import study_feedback
            result["learning_feedback"] = study_feedback.summarize(self.planner_dir, result["date"])
        return result

    def add_focus_time(self, result):
        """Pomodoro time recorded against each task of the shown plans (read only)."""
        for day, tasks in ((result.get("date"), (result.get("plan") or {}).get("tasks", [])),
                           ((result.get("previous") or {}).get("date"), (result.get("previous") or {}).get("tasks", []))):
            if day and tasks:
                spent = focus.plan_seconds(self.planner_dir, day)
                for task in tasks:
                    if isinstance(task, dict) and spent.get(task.get("id")):
                        task["focus_seconds"] = spent[task["id"]]
        return result

    async def progress(self, request: Request):
        data = await self.body(request)
        day = self.text(data, "date")
        feedback = {key: data[key] for key in ("actual_minutes", "understanding") if key in data}
        states = await self.planned(planner.set_state, day, data.get("task"), data.get("state"), **feedback)
        latest = await self.planned(planner.view, day)
        return {"date": day, "states": states, "derived": latest.get("derived", {}),
                "feedback": latest.get("feedback", {})}

    # -- pomodoro timer and focus statistics (planner folder only, never a tool) --

    async def focused(self, function, *args, **kwargs):
        def work():
            try:
                return function(self.planner_dir, *args, **kwargs)
            except focus.FocusError as exc:
                raise BadRequest(str(exc))
        return await run_in_threadpool(work)

    async def focus_state(self, request: Request):
        return await self.focused(focus.state)

    async def focus_action(self, request: Request):
        data = await self.body(request)
        action = data.get("action")
        if action == "start":
            return await self.focused(focus.start, data.get("mode"), data.get("task"), data.get("minutes"))
        if action in ("pause", "resume"):
            return await self.focused(getattr(focus, action))
        if action == "stop":
            return await self.focused(focus.stop, data.get("reason"))
        if action == "discard":
            return await self.focused(focus.stop, save=False)
        if action == "task":
            return await self.focused(focus.set_task, data.get("task"))
        raise BadRequest("action 只能是 start、pause、resume、stop、discard 或 task。")

    async def focus_settings(self, request: Request):
        return await self.focused(focus.update_settings, (await self.body(request)).get("changes"))

    async def focus_stats(self, request: Request):
        q = request.query_params
        return await self.focused(focus.stats, q.get("period", "week"), q.get("date") or None,
                                  q.get("start") or None, q.get("end") or None)

    async def focus_history(self, request: Request):
        q = request.query_params
        return await self.focused(focus.history, q.get("start") or None, q.get("end") or None,
                                  q.get("course") or None, q.get("q") or None)

    async def focus_add(self, request: Request):
        data = await self.body(request)
        return await self.focused(focus.add_manual, data.get("start"), data.get("minutes"), data.get("task"),
                                  data.get("note"))

    async def focus_edit(self, request: Request):
        data = await self.body(request)
        return await self.focused(focus.edit, self.text(data, "id"), data.get("changes"))

    async def focus_delete(self, request: Request):
        data = await self.body(request)
        self.confirmed(data)
        return await self.focused(focus.delete, self.text(data, "id"))

    async def focus_export(self, request: Request):
        return await self.focused(focus.export)

    async def focus_import(self, request: Request):
        data = await self.body(request)
        self.confirmed(data)
        return await self.focused(focus.import_records, data.get("data"))

    async def deadlines(self, request: Request):
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            raise BadRequest("days 必须是数字。")
        if not 1 <= days <= 366:
            raise BadRequest("days 必须在 1 到 366 之间。")
        return await self.call("upcoming_deadlines", days=days)

    async def courses(self, request: Request):
        which = request.query_params.get("which", "inprogress")
        if which not in COURSE_LISTS:
            raise BadRequest("which 只能是 inprogress、past、future 或 all。")
        result = await self.call("list_courses", which=which)
        if not isinstance(result, list):
            return result
        local = {e["moodle_id"]: e for e in self.entries()[0] if e["moodle_id"] is not None}
        for course in result:
            if isinstance(course, dict):
                course["local"] = local.get(course.get("id"))
        finished = {i for i, e in local.items() if e["completed"]}
        if which == "inprogress":
            # Moodle still lists a finished block course as running; the course map knows better.
            return [c for c in result if not (isinstance(c, dict) and c.get("id") in finished)]
        if which == "past" and finished - {c.get("id") for c in result if isinstance(c, dict)}:
            running = await self.call("list_courses", which="inprogress")
            for course in running if isinstance(running, list) else []:
                if isinstance(course, dict) and course.get("id") in finished:
                    course["local"] = local[course["id"]]
                    result.append(course)
        return result

    async def activities(self, request: Request):
        return await self.call("list_activities", course=str(request.path_params["course"]))

    async def assignments(self, request: Request):
        return await self.call("assignments", course=str(request.path_params["course"]))

    async def grades(self, request: Request):
        course = request.query_params.get("course", "")
        if course and not course.isdigit():
            raise BadRequest("course 必须是课程编号。")
        return await self.call("grades", course=course)

    async def files(self, request: Request):
        return await self.call("list_files", url=self.text(await self.body(request), "url"))

    async def page(self, request: Request):
        return await self.call("read_page", url=self.text(await self.body(request), "url"))

    async def download(self, request: Request):
        data = await self.body(request)
        url, dest_dir = self.text(data, "url"), self.folder(data)
        self.confirmed(data)
        name = "artemis_download" if url.startswith(ARTEMIS_PREFIX) else "download"
        return await self.call(name, url=url, dest_dir=dest_dir)

    async def artemis_courses(self, request: Request):
        return await self.call("artemis_courses")

    async def artemis_lectures(self, request: Request):
        return await self.call("artemis_lectures", course=str(request.path_params["course"]))

    async def artemis_exercises(self, request: Request):
        """Exercises with clone URLs, plus the user's own ticks from the planner folder."""
        course = request.path_params["course"]
        result = await self.call("artemis_exercise_repos", course=str(course))
        if not isinstance(result, list):
            return result
        try:
            checks = await run_in_threadpool(artemis_checks.course_checks, self.planner_dir, course)
            error = None
        except artemis_checks.CheckError as exc:
            checks, error = {}, str(exc)
        for exercise in result:
            if isinstance(exercise, dict):
                entry = checks.get(exercise.get("id"))
                exercise["checked"] = entry is not None
                exercise["checked_at"] = entry.get("at") if entry else None
        return {"course_id": course, "exercises": result, "checks_error": error}

    async def artemis_check(self, request: Request):
        data = await self.body(request)
        def work():
            try:
                return artemis_checks.set_done(self.planner_dir, data.get("course"), data.get("exercise"),
                                               data.get("done"), data.get("title", ""))
            except artemis_checks.CheckError as exc:
                raise BadRequest(str(exc))
        checks = await run_in_threadpool(work)
        return {"course_id": data.get("course"),
                "checked": {str(k): v.get("at") for k, v in checks.items()}}

    async def course_map(self, request: Request):
        entries, error = self.entries()
        for entry in entries:
            for key in ("course_dir", "moodle_dest", "artemis_dest"):
                entry[key + "_exists"] = bool(entry[key]) and Path(entry[key]).is_dir()
        return {"root": str(self.root), "inbox": str(self.root / "_Inbox"),
                "map_path": str(self.map_path), "entries": entries, "error": error}

    async def sync(self, request: Request):
        data = await self.body(request)
        source = data.get("source")
        if source not in ("moodle", "artemis"):
            raise BadRequest("source 只能是 moodle 或 artemis。")
        course = data.get("course")
        if not isinstance(course, int) or isinstance(course, bool) or course <= 0:
            raise BadRequest("course 必须是课程编号。")
        dest_dir = self.folder(data)
        dry_run = data.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise BadRequest("dry_run 必须是 true 或 false。")
        if not dry_run:
            self.confirmed(data)
        name = "sync_course" if source == "moodle" else "artemis_sync"
        result = await self.call(name, course=str(course), dest_dir=dest_dir, dry_run=dry_run)
        if not dry_run and isinstance(result, dict) and isinstance(result.get("artifacts"), list):
            entries, _ = self.entries()
            entry = next((e for e in entries if e.get(source + "_id") == course), None)
            if entry:
                import study_materials
                key = entry["folder"].split(" ", 1)[0]
                try:
                    result["materials_queue"] = await run_in_threadpool(study_materials.ingest, self.planner_dir,
                                                                       result, dest_dir, source, key)
                except (ValueError, OSError, RuntimeError):
                    result["materials_queue"] = {"error": "文件已同步，但材料索引未更新；下次同步可重新导入。"}
        return result

    async def open_folder(self, request: Request):
        if request.scope.get("tailnet"):
            raise BadRequest("文件夹只能在 Windows 电脑上打开（从 Mac 打开会弹在 Windows 屏幕上）。")
        path = Path(self.text(await self.body(request), "path"))
        try:
            resolved = path.resolve(strict=True)
            inside = resolved.is_relative_to(self.root.resolve(strict=True))
        except (OSError, RuntimeError):
            resolved, inside = None, False
        # Folders only: opening a file could run a program.
        if not inside or not resolved.is_dir():
            raise BadRequest(f"只能打开 {self.root} 里已存在的文件夹。")
        if self.opener is None:
            raise BadRequest("这台电脑不支持打开文件夹。")
        await run_in_threadpool(self.opener, str(resolved))
        return {"ok": True}


def _note(text: str):
    """One line for the app-mode log (ui.log); never includes the URL token or session values."""
    if sys.stderr is not None:
        print(time.strftime("%H:%M:%S"), text, file=sys.stderr, flush=True)


class Presence:
    """Counts open pages: each holds one /api/alive connection until it is closed."""

    def __init__(self):
        self._lock = threading.Lock()
        self.open = 0
        self.ever = False
        self._closed_at = None

    def enter(self):
        with self._lock:
            self.open += 1
            self.ever = True
            self._closed_at = None
            _note(f"page connected ({self.open} open)")

    def leave(self):
        with self._lock:
            self.open -= 1
            if self.open == 0:
                self._closed_at = time.monotonic()
            _note(f"page closed ({self.open} open)")

    def idle_for(self) -> float | None:
        """Seconds since the last page closed; None while one is open or before the first."""
        with self._lock:
            return None if self._closed_at is None else time.monotonic() - self._closed_at


class AliveApp:
    """Raw ASGI (a class, so Route does not wrap it): answer at once, then hold the
    response open until the page goes away; uvicorn reports that as http.disconnect."""

    def __init__(self, presence: Presence):
        self.presence = presence

    async def __call__(self, scope, receive, send):
        self.presence.enter()
        try:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"ok\n", "more_body": True})
            while (await receive())["type"] != "http.disconnect":
                pass
        finally:
            self.presence.leave()


CODE_DIR = Path(__file__).resolve().parent
BOOT_ID = secrets.token_hex(8)  # changes with every start, so an open page sees a restart


def fingerprint(folder: Path, pattern: str) -> str:
    """Short hash of the names, sizes and modification times of the files in folder (not recursive)."""
    digest = hashlib.sha256()
    for path in sorted(Path(folder).glob(pattern)):
        try:
            if not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}|{info.st_mtime_ns}|{info.st_size};".encode())
    return digest.hexdigest()[:16]


def broken_module(folder: Path = CODE_DIR) -> str | None:
    """Name of the first project module that does not compile (a half-saved edit), else None."""
    for path in sorted(Path(folder).glob("*.py")):
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OSError):
            return path.name
    return None


async def version(web_dir: Path) -> dict:
    """What the page compares to reload itself: this start, and the page files on disk."""
    return {"boot": BOOT_ID, "web": await run_in_threadpool(fingerprint, web_dir, "*")}


def _endpoint(handler):
    async def endpoint(request: Request):
        try:
            return JSONResponse(await handler(request))
        except BadRequest as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return endpoint


def create_app(tools: dict, *, token: str, port: int, map_path: Path = course_map.DEFAULT_MAP,
               opener=None, web_dir: Path = WEB_DIR, planner_dir: Path = planner.DEFAULT_DIR,
               tailnet: Tailnet | None = None):
    api = Api(tools, map_path, opener, planner_dir)
    presence = Presence()
    routes = [
        Route("/api/alive", AliveApp(presence)),
        Route("/api/version", _endpoint(lambda request: version(web_dir))),
        Route("/api/plan", _endpoint(api.plan)),
        Route("/api/progress", _endpoint(api.progress), methods=["POST"]),
        Route("/api/focus", _endpoint(api.focus_state)),
        Route("/api/focus/action", _endpoint(api.focus_action), methods=["POST"]),
        Route("/api/focus/settings", _endpoint(api.focus_settings), methods=["POST"]),
        Route("/api/focus/stats", _endpoint(api.focus_stats)),
        Route("/api/focus/history", _endpoint(api.focus_history)),
        Route("/api/focus/records", _endpoint(api.focus_add), methods=["POST"]),
        Route("/api/focus/records/edit", _endpoint(api.focus_edit), methods=["POST"]),
        Route("/api/focus/records/delete", _endpoint(api.focus_delete), methods=["POST"]),
        Route("/api/focus/export", _endpoint(api.focus_export)),
        Route("/api/focus/import", _endpoint(api.focus_import), methods=["POST"]),
        Route("/api/deadlines", _endpoint(api.deadlines)),
        Route("/api/courses", _endpoint(api.courses)),
        Route("/api/courses/{course:int}/activities", _endpoint(api.activities)),
        Route("/api/courses/{course:int}/assignments", _endpoint(api.assignments)),
        Route("/api/grades", _endpoint(api.grades)),
        Route("/api/files", _endpoint(api.files), methods=["POST"]),
        Route("/api/page", _endpoint(api.page), methods=["POST"]),
        Route("/api/download", _endpoint(api.download), methods=["POST"]),
        Route("/api/artemis/courses", _endpoint(api.artemis_courses)),
        Route("/api/artemis/{course:int}/lectures", _endpoint(api.artemis_lectures)),
        Route("/api/artemis/{course:int}/exercises", _endpoint(api.artemis_exercises)),
        Route("/api/artemis/check", _endpoint(api.artemis_check), methods=["POST"]),
        Route("/api/map", _endpoint(api.course_map)),
        Route("/api/sync", _endpoint(api.sync), methods=["POST"]),
        Route("/api/open-folder", _endpoint(api.open_folder), methods=["POST"]),
        Mount("/", StaticFiles(directory=web_dir, html=True)),
    ]
    app = Starlette(routes=routes)
    app.state.api = api
    app.state.presence = presence
    return LocalOnly(app, port, token, tailnet)


PREFERRED_PORT = 47823  # a stable address keeps the browser's notification permission between starts


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def pick_port() -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", PREFERRED_PORT))
        except OSError:
            return free_port()
    return PREFERRED_PORT


RESUME_ENV = "TUM_UI_RESUME"  # "<port>:<token>", handed from a restarting copy to its successor only
RESTART_SETTLE = 2  # seconds the program files must stay unchanged before restarting (editors save in steps)


def take_resume() -> tuple[int, str] | None:
    """Port and token from the copy that restarted us; removed at once so later children never see it."""
    port, _, token = os.environ.pop(RESUME_ENV, "").partition(":")
    if port.isdigit() and 0 < int(port) < 65536 and re.fullmatch(r"[\w-]{20,}", token):
        return int(port), token
    return None


def wait_for_port(port: int, seconds: float) -> bool:
    """True once 127.0.0.1:port can be bound (the old copy has let go of it)."""
    deadline = time.monotonic() + seconds
    while True:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return True
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def relaunch(port: int, token: str):
    """Start a fresh copy with the same command line; it takes over the port and token."""
    env = dict(os.environ, **{RESUME_ENV: f"{port}:{token}"})
    subprocess.Popen([sys.executable, *sys.orig_argv[1:]], env=env, cwd=str(CODE_DIR), close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


CLOSE_GRACE = 20         # app mode: seconds with no open page before quitting (a reload is faster)
FIRST_WINDOW_WAIT = 120  # app mode: give up if no page ever connects


def main(argv, tools: dict):
    import uvicorn

    parser = argparse.ArgumentParser(prog="tum_moodle_auto.py ui", description="本机网页界面")
    parser.add_argument("--port", type=int, default=0, help="端口（默认自动选一个空闲端口）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--map", type=Path, default=course_map.DEFAULT_MAP, help="课程对照表路径")
    parser.add_argument("--planner", type=Path, default=planner.DEFAULT_DIR, help="学习计划文件夹")
    parser.add_argument("--app", action="store_true",
                        help="桌面快捷方式用：没有终端，页面在 Chrome 标签页里打开，关掉标签页即退出")
    parser.add_argument("--stay", action="store_true",
                        help="和 --app 一起用：常驻后台，关掉所有页面也不退出")
    parser.add_argument("--tailscale", action="store_true",
                        help="也接受经 `tailscale serve` 从自己的 Tailscale 账号来的访问（如 Mac）")
    args = parser.parse_args(argv)
    if args.app:
        return run_app(args, tools)

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    port = args.port or pick_port()
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}/#t={token}"
    tailnet = tailnet_identity() if args.tailscale else None
    app = create_app(tools, token=token, port=port, map_path=args.map, planner_dir=args.planner,
                     tailnet=tailnet)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))

    def announce():
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        print("TUM Moodle 小助理已启动（只有这台电脑能访问）。")
        print(f"如果浏览器没有自动打开，请复制这个地址：\n  {url}")
        if tailnet:
            print(f"同一 Tailscale 账号的其他设备（如 Mac）：http://{max(tailnet.hosts, key=len)}/"
                  + ("" if port == PREFERRED_PORT else f"（但这次端口是 {port}，tailscale serve 指向 {PREFERRED_PORT}，连不上）"))
        elif args.tailscale:
            print("没有读到 Tailscale 状态（没装、没运行或没登录）：这次只有本机能访问。")
        print("用完关掉这个窗口即可退出；登录状态随之清除。")
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    server.run()


def run_app(args, tools: dict):
    """No console (pythonw): one server per user, the page in a Chrome tab, quit when all its tabs close."""
    import traceback

    import uvicorn

    import app_window

    instance = app_window.SingleInstance()
    if instance.running_elsewhere:
        instance.ask_to_show()  # the running copy opens another tab
        instance.close()
        return
    app_window.quiet_output()  # only the running copy owns ui.log
    server = None
    restart = []
    try:
        # After a program update the old copy restarts us with its port and token, so the
        # open tabs keep working and only reload; no new tab is opened.
        resume = take_resume()
        if resume and not wait_for_port(resume[0], 15):
            _note("restart: the old port stayed busy; starting on a new address")
            resume = None
        if resume:
            port, token = resume
            _note("restarted after a program update")
        else:
            port = args.port or pick_port()
            token = secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{port}/#t={token}"
        tailnet = tailnet_identity() if args.tailscale else None
        if args.tailscale:
            _note("tailnet access on" if tailnet else "tailscale status unavailable: this computer only")
            if tailnet and port != PREFERRED_PORT:
                _note(f"port {port} is not {PREFERRED_PORT}: `tailscale serve` will not reach this copy")
        app = create_app(tools, token=token, port=port, map_path=args.map, planner_dir=args.planner,
                         tailnet=tailnet)
        presence = app.app.state.presence
        api = app.app.state.api
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                               access_log=False, timeout_graceful_shutdown=3))

        def lifetime():
            started = time.monotonic()
            while not server.started:
                if time.monotonic() - started > 30 or server.should_exit:
                    return
                time.sleep(0.1)
            if resume:
                _note(f"server started on port {port}; waiting for the open pages to reconnect")
            elif args.no_browser:
                _note(f"server started on port {port}; no tab until the shortcut is used")
            else:
                _note(f"server started on port {port}; opening the page")
                app_window.open_window(url)
            opened = time.monotonic()
            code = fingerprint(CODE_DIR, "*.py")
            pending, pending_since = None, 0.0
            while not server.should_exit:
                current = fingerprint(CODE_DIR, "*.py")
                if current == code:
                    pending = None
                elif current != pending:
                    pending, pending_since = current, time.monotonic()
                elif time.monotonic() - pending_since >= RESTART_SETTLE:
                    broken = broken_module()
                    if broken:
                        _note(f"program files changed but {broken} does not compile: not restarting")
                        code, pending = current, None  # try again after the next change
                    elif api.lock.acquire(blocking=False):  # never in the middle of a sync or download
                        _note("program files changed: restarting")
                        restart.append(True)
                        server.should_exit = True
                        break
                if instance.wait_for_show(1000):
                    _note("started again: opening another tab")
                    app_window.open_window(url)
                if args.stay:
                    continue  # resident: keeps running with no page open (other devices may come)
                idle = presence.idle_for()
                if idle is not None and idle >= CLOSE_GRACE:
                    _note(f"no open page for {CLOSE_GRACE} s: quitting")
                    server.should_exit = True
                elif not presence.ever and time.monotonic() - opened > FIRST_WINDOW_WAIT:
                    server.should_exit = True
                    if resume:  # the tabs were closed while we restarted
                        _note("restart: no page came back; quitting")
                        break
                    app_window.message("小助理的页面没能在浏览器里打开，已退出。可以再双击一次快捷方式试试。", error=True)

        threading.Thread(target=lifetime, daemon=True).start()
        server.run()
        if not server.started:
            raise RuntimeError("本机服务没能启动")
        if restart:
            instance.close()  # the successor must become the running copy
            relaunch(port, token)
    except BaseException as exc:  # uvicorn reports a failed bind as SystemExit
        if isinstance(exc, KeyboardInterrupt):
            return
        traceback.print_exc()
        app_window.message(f"小助理没能启动：{str(exc)[:300] or type(exc).__name__}\n\n"
                           f"详细信息在 {app_window.LOG_FILE}", error=True)
    finally:
        instance.close()
