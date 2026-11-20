"""Read-only Moodle course-file synchronization that mirrors the online files.

Local copies follow the course (2026-11-15, user's request): an updated file
replaces the local one and a file removed online leaves the folder. Neither is
deleted outright: both go to the Windows Recycle Bin after the update has been
committed. A local file the user changed (e.g. annotated) is never discarded:
when the online file changes too, the user's copy is kept as "<name> (批注)".

Also used for Artemis: a client may set ``sync_hosts`` (allowed file hosts) and
``sync_state_name`` (its own state file name); Moodle's values are the defaults.
A client with ``sync_layout = "kind"`` gets kind/file paths (Slides, Exercises,
Exams, Other) instead of mirroring course sections, so several courses and
platforms can share one directory, each with its own state file.
Only the supplied client's guarded session performs HTTP requests.  This module
does not read credentials, and refuses to persist session-bearing metadata.  The
actual file move uses Windows' no-overwrite rename semantics, matching this
project's supported platform; previews and parsers are platform independent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from urllib.parse import parse_qsl, unquote, unquote_to_bytes, urlsplit
from uuid import uuid4


# This module is also loaded by absolute path under Python -I in offline tests.
_lock_spec = importlib.util.spec_from_file_location(
    "_tum_directory_lock", Path(__file__).with_name("local_lock.py"))
_lock_module = importlib.util.module_from_spec(_lock_spec)
_lock_spec.loader.exec_module(_lock_module)
directory_lock = _lock_module.directory_lock
DirectoryBusyError = _lock_module.DirectoryBusyError

_policy_spec = importlib.util.spec_from_file_location(
    "_tum_entrepreneurship", Path(__file__).with_name("entrepreneurship.py"))
entrepreneurship = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(entrepreneurship)


STATE_NAME = ".moodle-sync.json"
# State files, never synchronized file names: .moodle-sync[-<course id>].json, .artemis-sync[-<id>].json
_STATE_FILE = re.compile(r"\.(?:moodle|artemis)-sync(?:-\d+)?\.json", re.I)
KIND_FOLDERS = ("Slides", "Exercises", "Exams", "Other")
DEFAULT_HOSTS = frozenset({"www.moodle.tum.de"})
STATE_VERSION = 2
MAX_PATH_UNITS = 240
MAX_COMPONENT_UNITS = 100
BASE_PATH_UNITS = MAX_PATH_UNITS - 20  # Reserve room for backups and .part names.
MIN_SECRET_LENGTH = 8  # Same threshold as tum_moodle_auto.session_secrets.
_SYNC_LOCK = RLock()
KINDS = ("new", "updated", "unchanged", "removed_online", "failed", "local_modified",
         "trashed", "kept_local", "renamed")
# Courses whose single-file resources are named after the Moodle activity, because
# the uploaded names are arbitrary (Analysis, 2026-11-15: "LA_Sheets (31).pdf" is
# Sheet 2). Their tracked files are moved to the current name on the next sync.
ACTIVITY_NAMED_COURSES = frozenset({121121})
_ENTRY_KEYS = {
    "course_id", "cmid", "folder_path", "filename", "url", "etag",
    "last_modified", "local_path", "last_sync",
}
_V2_KEYS = _ENTRY_KEYS | {"sha256", "size", "history"}
_HASH = re.compile(r"[0-9a-f]{64}")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
_RESERVED.update(f"{prefix}{number}" for prefix in ("COM", "LPT")
                 for number in (*range(1, 10), "¹", "²", "³"))
_AUTH_KEY = re.compile(r"(?:sesskey|cookie|moodlesession|token|jwt|password|secret|samlresponse)", re.I)


class SyncError(ValueError):
    """An explanation containing only fixed, safe text."""

    def __init__(self, message, *, code="sync_failed", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _secrets(client) -> set[str]:
    """Only inspect already-loaded memory; never access a lazy sesskey property."""
    found = {getattr(client, "_sesskey", None)}
    cookies = getattr(getattr(client, "session", None), "cookies", ())
    for cookie in cookies:
        found.add(getattr(cookie, "value", None))
    # Ignore short values (e.g. a cookie "1"): they are not session secrets and
    # would make every downloaded file look like it contained one.
    return {value for value in found if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH}


def _check_secrets(value, secrets):
    secrets = {variant for secret in secrets for variant in
               (secret, unquote(secret), unquote(unquote(secret))) if variant}
    if isinstance(value, dict):
        for key, item in value.items():
            _check_secrets(key, secrets)
            _check_secrets(item, secrets)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _check_secrets(item, secrets)
        return
    if not isinstance(value, str):
        return
    variants = {value, unquote(value), unquote(unquote(value))}
    if any(secret in candidate for secret in secrets for candidate in variants):
        raise SyncError("同步资料含会话信息，已拒绝保存；请重新获取普通文件链接。")


def _safe_url(url, secrets, hosts=DEFAULT_HOSTS):
    if not isinstance(url, str) or not url or "\\" in url or any(c.isspace() for c in url):
        raise SyncError("文件链接不符合安全要求。")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname in hosts
                 and parts.port in (None, 443) and parts.username is None
                 and parts.password is None and not parts.fragment)
    except ValueError:
        valid = False
    if not valid or any(ord(c) < 32 for c in url):
        raise SyncError("文件链接不符合安全要求。")
    for key, _ in parse_qsl(parts.query, keep_blank_values=True):
        if _AUTH_KEY.search(unquote(key)):
            raise SyncError("文件链接含登录或会话参数，已拒绝保存。")
    _check_secrets(url, secrets)
    return url


def _units(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _shorten(text, limit):
    if _units(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    extension = PurePosixPath(text).suffix
    if _units(extension) > 18:
        extension = ""
    tail = "_" + digest + extension
    if _units(tail) > limit:
        tail = "_" + digest
    prefix = text[:-len(extension)] if extension else text
    while prefix and _units(prefix + tail) > limit:
        prefix = prefix[:-1]
    return prefix.rstrip(". ") + tail


def safe_name(value, limit=MAX_COMPONENT_UNITS):
    """A single Windows-safe component; never interpret a name as a path."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value or ""))
    text = text.rstrip(". ") or "_"
    if text.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        text = "_" + text
    return _shorten(text, limit)


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _exists(path):
    return path.exists() or _is_link(path)


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise SyncError("同步记录的本地路径不安全；原记录未被修改。")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise SyncError("同步记录的本地路径不安全；原记录未被修改。")
    for part in path.parts:
        if part in ("", ".", "..") or safe_name(part) != part:
            raise SyncError("同步记录的本地路径不安全；原记录未被修改。")
    if (_STATE_FILE.fullmatch(path.parts[0]) or
            re.match(r"\.(?:moodle|artemis)-sync(?:-\d+)?\.json\.", path.parts[0], re.I)):
        raise SyncError("同步记录的本地路径与状态文件冲突；原记录未被修改。")
    return path


def _target(root, relative):
    if _is_link(root):
        raise SyncError("同步目录是链接或 Windows 联接，已拒绝写入。")
    rel = _relative(relative)
    candidate = root.joinpath(*rel.parts)
    if _units(str(candidate)) > MAX_PATH_UNITS:
        raise SyncError("本地路径过长，请选择更短的同步目录。")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (ValueError, OSError, RuntimeError):
        raise SyncError("同步路径超出目标目录，已拒绝写入。") from None
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if _is_link(cursor):
            raise SyncError("同步路径包含链接或 Windows 联接，已拒绝写入。")
    return candidate


def _identity(course_id, cmid, folder_path, filename):
    return json.dumps([course_id, cmid, folder_path, filename], ensure_ascii=False,
                      separators=(",", ":"))


def _validate_entry(root, identity, entry, course_id, secrets, hosts, version=2):
    expected = _ENTRY_KEYS if version == 1 else _V2_KEYS
    if version == 3 and isinstance(entry, dict) and "alias_of" in entry:
        expected = expected | {"alias_of"}
    if not isinstance(entry, dict) or set(entry) != expected:
        raise ValueError("entry schema")
    if (type(entry["course_id"]) is not int or entry["course_id"] <= 0
            or entry["course_id"] != course_id
            or type(entry["cmid"]) is not int or entry["cmid"] <= 0):
        raise ValueError("identity ids")
    for key in ("folder_path", "filename", "url", "local_path", "last_sync"):
        if not isinstance(entry[key], str):
            raise ValueError("entry strings")
    if not entry["filename"] or not entry["last_sync"]:
        raise ValueError("empty fields")
    for key in ("etag", "last_modified"):
        if entry[key] is not None and not isinstance(entry[key], str):
            raise ValueError("header type")
    if identity != _identity(entry["course_id"], entry["cmid"], entry["folder_path"], entry["filename"]):
        raise ValueError("identity mismatch")
    _safe_url(entry["url"], secrets, hosts)
    _target(root, entry["local_path"])
    if version == 1:
        entry.update(sha256=None, size=None, history=[])
    if ((entry["sha256"] is None) != (entry["size"] is None)
            or (entry["sha256"] is not None and
                (not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"])
                 or type(entry["size"]) is not int or entry["size"] < 0))
            or not isinstance(entry["history"], list)):
        raise ValueError("content digest")
    for previous in entry["history"]:
        if (not isinstance(previous, dict) or set(previous) != {"sha256", "size", "local_path"}
                or not isinstance(previous["sha256"], str) or not _HASH.fullmatch(previous["sha256"])
                or type(previous["size"]) is not int or previous["size"] < 0):
            raise ValueError("history")
        _target(root, previous["local_path"])
    _check_secrets(entry, secrets)


def _json_pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _alias_groups(state):
    groups = {}
    for identity, entry in state["files"].items():
        groups.setdefault(entry["local_path"].casefold(), []).append((identity, entry))
    return groups.values()


def _normalize_aliases(state):
    for group in _alias_groups(state):
        canonical, owner = min(group, key=lambda pair: (pair[1]["cmid"], pair[0]))
        for identity, entry in group:
            if (entry["sha256"], entry["size"]) != (owner["sha256"], owner["size"]):
                raise SyncError("共用资料的内容版本不一致，已停止保存。")
            entry.pop("alias_of", None)
            if identity != canonical:
                entry["alias_of"] = canonical


def _validate_aliases(state):
    for group in _alias_groups(state):
        canonical, owner = min(group, key=lambda pair: (pair[1]["cmid"], pair[0]))
        for identity, entry in group:
            if (entry.get("alias_of") != (canonical if identity != canonical else None)
                    or (entry["sha256"], entry["size"]) != (owner["sha256"], owner["size"])):
                raise ValueError("invalid alias")
            if len(group) > 1 and (entry["cmid"] not in entrepreneurship.READINGS
                                   or entry["filename"] != owner["filename"] or entry["sha256"] is None):
                raise ValueError("unregistered shared material")


def _read_state(root, secrets, hosts=DEFAULT_HOSTS, state_name=STATE_NAME):
    path = root / state_name
    if _is_link(path):
        raise SyncError("同步状态文件是链接，已拒绝读取和覆盖。")
    if not path.exists():
        return {"version": STATE_VERSION, "course_id": None, "files": {}}, None
    try:
        payload = path.read_bytes()
        state = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_pairs)
        if (not isinstance(state, dict) or set(state) != {"version", "course_id", "files"}
                or type(state["version"]) is not int or state["version"] not in (1, 2, 3)
                or not isinstance(state["files"], dict)
                or (state["course_id"] is not None and
                    (type(state["course_id"]) is not int or state["course_id"] <= 0))):
            raise ValueError("schema")
        if state["version"] == 3 and state["course_id"] not in entrepreneurship.COURSES:
            raise ValueError("unregistered alias policy")
        paths = set()
        for identity, entry in state["files"].items():
            _validate_entry(root, identity, entry, state["course_id"], secrets, hosts, state["version"])
            folded = entry["local_path"].casefold()
            if folded in paths and state["version"] != 3:
                raise ValueError("duplicate local path")
            paths.add(folded)
        if state["version"] == 3:
            _validate_aliases(state)
        _check_secrets(state, secrets)
        state["version"] = max(STATE_VERSION, state["version"])
        return state, hashlib.sha256(payload).hexdigest()
    except SyncError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise SyncError("同步状态文件损坏或格式不认识；原文件未被覆盖，请先检查它。") from None


def _folder_parts(activity, file, url):
    if _field(activity, "type") != "folder":
        return []
    # Moodle's slash-argument URL: /context/mod_folder/content/itemid/sub/file.
    parts = urlsplit(url).path.split("/")
    try:
        start = parts.index("mod_folder")
        if parts[start + 1] == "content":
            return [unquote(part) for part in parts[start + 3:-1]]
    except (ValueError, IndexError):
        pass
    # Future client versions may carry an explicit relative folder path.
    folder = _field(file, "filepath", "")
    return [part for part in str(folder).strip("/").split("/") if part]


def _words(pattern):
    """Whole words between non-letters, so "DB_Exam_2023" matches exam but "Examples" does not."""
    return re.compile(r"(?<![^\W\d_])(?:" + pattern + r")(?![^\W\d_])", re.I)


_KIND_WORDS = (
    ("Exams", _words(r"exams?|(?:alt|probe)?klausur\w*|endterm|midterm|retake|(?:short|bonus)[ _-]?tests?|"
                     r"pr(?:ü|ue)fung\w*")),
    ("Exercises", _words(r"tutorials?|tutorium|exercises?|(?:ü|ue)bung\w*|sheets?|blatt|homework|hausaufgabe\w*|"
                         r"solutions?|l(?:ö|oe)sung\w*|assignments?|aufgabe\w*|problem ?sets?")),
    ("Slides", _words(r"slides?|folien?|lectures?|vorlesung\w*|lec\d*|chapters?|kapitel|skript|script|notes|handout")),
    ("Other", _words(r"organi[sz]ation\w*|organisatorisch\w*|syllabus|papers?|readings?|literature|"
                     r"code of conduct|conduct|commitment|guidelines?|rules|polic(?:y|ies)")),
)


def material_kind(texts, default="Other"):
    """Slides, Exercises, Exams or Other from names ordered most specific first.

    The first text with a known word decides (file name before folder, activity and
    section), so "Lecture Material and Tutorials/…/slides/x.pdf" is Slides.
    """
    for text in texts:
        for kind, pattern in _KIND_WORDS:
            if text and pattern.search(str(text)):
                return kind
    return default


def display_filename(activity, filename):
    """The local name of one file: the server's name, or for a single-file resource
    of an ACTIVITY_NAMED_COURSES course, the Moodle activity name plus the file's
    extension ("Sheet 2" + "LA_Sheets (31).pdf" -> "Sheet 2.pdf").

    Only presentation: the server's name stays part of the sync identity.
    """
    if (_field(activity, "course_id") not in ACTIVITY_NAMED_COURSES
            or _field(activity, "type") != "resource"):
        return filename
    name = str(_field(activity, "name", "") or "")
    suffix = PurePosixPath(filename).suffix
    # TODO(human): return the activity-based name, or filename when it is not usable.
    return filename  # Until then: the server's name, so nothing gets renamed.


def _layout(root, activity, folder_parts, filename, by_kind=False):
    if by_kind:
        policy = entrepreneurship.layout(activity, filename, folder_parts, material_kind)
        kind = _field(activity, "kind") or material_kind(
            [PurePosixPath(filename).stem, *reversed(folder_parts),
             _field(activity, "name", ""), _field(activity, "section", "")])
        if policy is not None:
            kind, filename = policy
        else:
            filename = display_filename(activity, filename)
        components = [kind, filename]
    else:
        components = [_field(activity, "section", "") or "General"]
        if _field(activity, "type") == "folder":
            components.append(_field(activity, "name", "") or "Folder")
            components.extend(folder_parts)
        components.append(filename)
    if len(components) > 64:
        raise SyncError("文件子目录过深，请使用更短、更浅的同步路径。")
    components = [safe_name(part) for part in components]
    budget = BASE_PATH_UNITS - _units(str(root)) - len(components)
    if budget < 10 * len(components):
        raise SyncError("本地路径过长，请选择更短的同步目录。")
    while sum(_units(part) for part in components) > budget:
        index = max(range(len(components)), key=lambda i: _units(components[i]))
        limit = _units(components[index]) - 1
        components[index] = _shorten(components[index], limit)
    return PurePosixPath(*components).as_posix()


def _numbered(root, relative):
    """relative, then "name (1).ext", "name (2).ext", ... within the path limits."""
    original = _target(root, relative)
    parent = PurePosixPath(relative).parent
    stem, suffix = original.stem, original.suffix
    for counter in range(10000):
        if counter:
            tail = f" ({counter})" + suffix
            limit = min(MAX_COMPONENT_UNITS, MAX_PATH_UNITS - _units(str(original.parent)) - 1)
            name = _shorten(stem, limit - _units(tail)) + tail
        else:
            name = original.name
        candidate = (parent / name).as_posix()
        yield candidate, _target(root, candidate)


def _unique(root, relative, reserved, identity=None):
    for candidate, target in _numbered(root, relative):
        owner = reserved.get(candidate.casefold())
        if owner is not None and owner != identity:
            continue
        if not _exists(target):
            return candidate
    raise SyncError("同名文件太多，未找到可用的本地文件名。")


def _annotated_relative(root, relative, reserved):
    """Where a user-changed copy is kept when the online file changes: "<name> (批注).ext"."""
    path = PurePosixPath(relative)
    tail = " (批注)" + path.suffix
    name = _shorten(path.stem, MAX_COMPONENT_UNITS - _units(tail)) + tail
    return _unique(root, (path.parent / name).as_posix(), reserved)


def _relocation(root, desired, reserved, identity, entry):
    """The current-layout path for a tracked file, and whether it is already there.

    Our own reservation is reusable; an unowned existing file is reusable only when
    it is exactly this entry's content (a move that finished before the state did).
    """
    for candidate, target in _numbered(root, desired):
        owner = reserved.get(candidate.casefold())
        if owner == identity:
            return candidate, False
        if owner is not None:
            continue
        if not _exists(target):
            return candidate, False
        if entry.get("sha256") is not None and _matches(target, entry):
            return candidate, True
    raise SyncError("同名文件太多，未找到可用的本地文件名。")


def _header(headers, name):
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return str(value) if value is not None else None
    return None


def _plain_etag(etag):
    """One Etag per file version: Apache adds "-gzip" when it compresses a GET
    (text files) but not for HEAD, which made every .txt look updated."""
    if not etag:
        return etag
    return re.sub(r'-(?:gzip|br|deflate|zstd)(?="?$)', "", etag.removeprefix("W/"))


def _check_response(response, url, secrets, filename, hosts=DEFAULT_HOSTS):
    response.raise_for_status()
    visited = [response, *getattr(response, "history", [])]
    for item in visited:
        final_url = getattr(item, "url", None) or url
        _safe_url(final_url, secrets, hosts)
        if "/login/" in urlsplit(final_url).path:
            raise SyncError("文件请求返回登录页面，请重新登录后再同步。")
    content_type = (_header(response.headers, "Content-Type") or "").split(";", 1)[0].lower()
    if content_type == "text/html" and not filename.lower().endswith((".html", ".htm")):
        raise SyncError("文件请求返回网页而非文件，未保存该响应。")
    result = {"etag": _plain_etag(_header(response.headers, "Etag")),
              "last_modified": _header(response.headers, "Last-Modified")}
    _check_secrets(result, secrets)
    return result


def _request(client, method, url, **kwargs):
    request = getattr(client, "sync_request", None)
    if callable(request):
        return request(method, url, **kwargs)
    return getattr(client.session, method.lower())(url, **kwargs)


def _authentication_failed(exc):
    return any(base.__name__ in ("NotLoggedIn", "SyncAuthError") for base in type(exc).__mro__)


def _close(response):
    if response is not None:
        try:
            response.close()
        except Exception:
            pass


def _metadata(client, url, secrets, filename, hosts=DEFAULT_HOSTS):
    response = None
    try:
        response = _request(client, "HEAD", url, allow_redirects=True,
                            timeout=getattr(client, "timeout", 30))
        return _check_response(response, url, secrets | _secrets(client), filename, hosts)
    except Exception as exc:
        if _authentication_failed(exc):
            raise
        pass  # Some Moodle installations do not support HEAD for pluginfile.php.
    finally:
        _close(response)
    response = None
    try:
        response = _request(client, "GET", url, stream=True, allow_redirects=True,
                            timeout=getattr(client, "timeout", 30))
        return _check_response(response, url, secrets | _secrets(client), filename, hosts)
    finally:
        _close(response)  # Never read the GET body used for metadata fallback.


def _move_no_replace(source, target):
    if os.name != "nt":
        raise SyncError("实际文件同步目前只支持 Windows；预览不受影响。")
    if _exists(target):
        raise SyncError("本地目标已存在，未覆盖任何文件。")
    os.rename(source, target)  # Windows rename atomically refuses an existing target.


def _recycle(path):
    """Move one file to the Windows Recycle Bin; never delete it permanently.

    Refuses drives without a Recycle Bin (anything but a local fixed disk), where
    the shell would delete for good. FOF_WANTNUKEWARNING asks before a permanent
    delete (e.g. a file too large for the bin) instead of doing it silently.
    """
    if "unittest" in sys.modules:  # Offline tests patch _trash; never touch the real bin.
        raise SyncError("测试中不使用真实回收站。", code="trash_failed")
    if os.name != "nt":
        raise SyncError("移到回收站目前只支持 Windows。", code="trash_failed")
    import ctypes
    from ctypes import wintypes

    absolute = os.path.abspath(path)
    drive = os.path.splitdrive(absolute)[0]
    if len(drive) != 2 or ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 3:  # DRIVE_FIXED
        raise SyncError("此磁盘没有回收站，已保留文件。", code="trash_failed")

    class FileOperation(ctypes.Structure):  # SHFILEOPSTRUCTW
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    # FO_DELETE; FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING.
    # pFrom is a double-NUL-terminated list; ctypes adds the second NUL.
    operation = FileOperation(None, 3, absolute + "\0", None, 0x4 | 0x10 | 0x40 | 0x400 | 0x4000,
                              False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted or os.path.lexists(absolute):
        raise SyncError("无法移到回收站（文件可能正被打开）；已保留，下次同步再试。",
                        code="trash_failed", retryable=True)


_trash = _recycle  # Tests replace this with a fake Recycle Bin.


def safe_download_chunks(response, client, initial_secrets=()):
    """Yield checked file bytes without reading credentials or a lazy sesskey.

    Buffer the first 64 KiB (or the complete smaller file) before yielding, so a
    login form split across network chunks never becomes a course-file download.
    Check known in-memory session values throughout the stream, including values
    split across chunks and up to two levels of percent encoding. The caller owns
    the response and must close it. A late failure may leave only a safe partial
    prefix that does not contain a complete detected cookie or sesskey.
    """
    known = set(initial_secrets) | _secrets(client)
    prefix = bytearray()
    tail = b""
    inspected = False
    prefix_limit = 1 << 16

    def check_login(data):
        lowered = bytes(data).lower()
        if b"<form" in lowered and (
                b"/login/index.php" in lowered
                or re.search(br"name\s*=\s*['\"]?(?:j_password|logintoken)\b", lowered)
                or re.search(br"(?:id|class)\s*=\s*['\"][^'\"]*\blogin[-_ ]?form\b", lowered)):
            raise SyncError("下载内容是登录页面，未保存为课程文件。")

    for chunk in response.iter_content(prefix_limit):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise SyncError("下载响应格式不正确，未保存该内容。")
        known.update(_secrets(client))
        values = {variant.encode("utf-8") for value in known if isinstance(value, str) and value
                  for variant in (value, unquote(value), unquote(unquote(value))) if variant}
        window = tail + chunk
        variants = (window, unquote_to_bytes(window),
                    unquote_to_bytes(unquote_to_bytes(window)))
        if any(value in candidate for value in values for candidate in variants):
            raise SyncError("下载响应含会话信息，已拒绝把该内容写入文件。")
        # A fully percent-encoded character can occupy 3 bytes, or at most 9
        # bytes with a second layer. Keeping this much raw tail also detects
        # sequences split inside a percent escape rather than only at characters.
        tail_size = max((len(value) * 9 - 1 for value in values), default=0)
        tail = window[-tail_size:] if tail_size else b""
        if inspected:
            yield chunk
            continue
        needed = prefix_limit - len(prefix)
        prefix.extend(chunk[:needed])
        if len(prefix) == prefix_limit:
            check_login(prefix)
            yield bytes(prefix)
            prefix.clear()
            inspected = True
            remainder = chunk[needed:]
            if remainder:
                yield remainder
    if not inspected:
        check_login(prefix)
        if prefix:
            yield bytes(prefix)


def _backup_relative(root, relative, reserved):
    target = _target(root, relative)
    tail = "_old" + target.suffix
    limit = MAX_COMPONENT_UNITS - _units(tail)
    if limit < 9:
        raise SyncError("文件扩展名过长，无法安全保留旧版本；请改用较短的文件名。")
    old_name = _shorten(target.stem, limit) + tail
    old_relative = (PurePosixPath(relative).parent / old_name).as_posix()
    return _unique(root, old_relative, reserved)


def _fingerprint(path):
    if not _exists(path):
        return None
    if _is_link(path) or not path.is_file():
        raise SyncError("本地目标不是普通文件，已拒绝读取。", code="unsafe_path")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def _matches(path, entry):
    return _fingerprint(path) == {key: entry[key] for key in ("sha256", "size")}


def _download(client, root, relative, url, metadata, secrets, hosts=DEFAULT_HOSTS, text=None):
    """Stage checked bytes only; journaled commit below owns all final renames."""
    target = _target(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    _target(root, relative)  # Check again after creating parents.
    temporary = target.with_name(_shorten(target.name, MAX_COMPONENT_UNITS - 14)
                                 + "." + uuid4().hex[:8] + ".part")
    if _units(str(temporary)) > MAX_PATH_UNITS + 14:
        raise SyncError("下载临时路径过长，请选择更短的同步目录。")
    response = None
    try:
        if text is None:
            response = _request(client, "GET", url, stream=True, allow_redirects=True,
                                timeout=getattr(client, "timeout", 30))
            current = _check_response(response, url, secrets | _secrets(client), target.name, hosts)
            # Missing download headers may use the successful HEAD metadata.
            current = {key: current[key] if current[key] is not None else metadata[key]
                       for key in ("etag", "last_modified")}
            chunks = iter(safe_download_chunks(response, client, initial_secrets=secrets))
        else:
            _check_secrets(text, secrets | _secrets(client))
            current = dict(metadata)
            chunks = iter([text.encode("utf-8")])
        _check_secrets(current, secrets | _secrets(client))
        first = next(chunks, b"")  # Validate initial HTML/secret bytes before even creating .part.
        digest = hashlib.sha256()
        size = 0
        with temporary.open("xb") as handle:
            if first:
                handle.write(first)
                digest.update(first)
                size += len(first)
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if current["last_modified"]:
            try:
                timestamp = parsedate_to_datetime(current["last_modified"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                seconds = timestamp.timestamp()
            except (ValueError, TypeError, OverflowError):
                pass  # An invalid server date does not invalidate the file bytes.
            else:
                os.utime(temporary, (seconds, seconds))
        return current, temporary.relative_to(root).as_posix(), {"sha256": digest.hexdigest(), "size": size}
    finally:
        _close(response)


def _write_state(root, state, secrets, expected_signature, state_name=STATE_NAME):
    if state["version"] == 3:
        _normalize_aliases(state)
    _check_secrets(state, secrets)
    path = root / state_name
    if _is_link(root) or _is_link(path):
        raise SyncError("同步目录或状态文件变成了链接，已拒绝写入状态。")
    root.mkdir(parents=True, exist_ok=True)
    if _is_link(root) or _is_link(path):
        raise SyncError("同步目录或状态文件变成了链接，已拒绝写入状态。")
    actual_signature = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    if actual_signature != expected_signature:
        raise SyncError("同步记录在运行期间发生变化，未覆盖；请停止并行同步后重试。")
    temporary = root / (state_name + "." + uuid4().hex[:8] + ".part")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    # Only the validated synchronization metadata is replaced, never a course file.
    written_signature = hashlib.sha256(temporary.read_bytes()).hexdigest()
    os.replace(temporary, path)
    return written_signature


def _journal_path(root, state_name):
    path = root / (state_name + ".journal.json")
    if _is_link(root) or _is_link(path):
        raise SyncError("同步恢复记录是链接，已停止。", code="unsafe_journal")
    return path


def _save_journal(root, state_name, journal, secrets):
    _check_secrets(journal, secrets)
    path = _journal_path(root, state_name)
    if path.exists():
        raise SyncError("存在尚未完成的同步恢复记录，请先恢复。", code="recovery_pending")
    temporary = path.with_name(path.name + "." + uuid4().hex[:8] + ".part")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(journal, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _move_no_replace(temporary, path)


def _read_journal(root, state_name, secrets, hosts):
    path = _journal_path(root, state_name)
    if not path.exists():
        return None
    try:
        journal = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs)
        if (not isinstance(journal, dict) or set(journal) != {
                "version", "identity", "entry", "previous", "temporary", "backup", "signature"}
                or type(journal["version"]) is not int or journal["version"] != 1
                or (journal["signature"] is not None and
                    (not isinstance(journal["signature"], str) or not _HASH.fullmatch(journal["signature"])))):
            raise ValueError("journal schema")
        entry = journal["entry"]
        entry_version = 3 if entry["course_id"] in entrepreneurship.COURSES else 2
        _validate_entry(root, journal["identity"], entry, entry["course_id"], secrets, hosts, entry_version)
        if entry["sha256"] is None:
            raise ValueError("journal missing digest")
        previous = journal["previous"]
        if previous is not None:
            _validate_entry(root, journal["identity"], previous, entry["course_id"], secrets, hosts, entry_version)
            if previous["local_path"] != entry["local_path"] or previous["sha256"] is None:
                raise ValueError("journal previous")
        temporary = _target(root, journal["temporary"])
        target = _target(root, entry["local_path"])
        expected_prefix = _shorten(target.name, MAX_COMPONENT_UNITS - 14)
        if (temporary == target or not re.fullmatch(re.escape(expected_prefix) + r"\.[0-9a-f]{8}\.part", temporary.name)):
            raise ValueError("journal temporary")
        if temporary.parent != target.parent:
            raise ValueError("journal temporary parent")
        if journal["backup"] is not None:
            backup = _target(root, journal["backup"])
            if (previous is None or backup.parent != temporary.parent or backup == temporary
                    or journal["backup"] == entry["local_path"]
                    or not any(p["local_path"] == journal["backup"] and
                               p["sha256"] == previous["sha256"] and p["size"] == previous["size"]
                               for p in entry["history"])):
                raise ValueError("journal backup")
        _check_secrets(journal, secrets)
        return journal
    except SyncError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise SyncError("同步恢复记录损坏或格式不认识，未改动文件。", code="invalid_journal") from None


def _finish_commit(root, state, signature, state_name, journal, secrets):
    """Idempotently complete one rename + state transaction after a process exit."""
    identity, entry, previous = (journal[key] for key in ("identity", "entry", "previous"))
    state_path = root / state_name
    if _is_link(state_path):
        raise SyncError("同步记录变成链接，未继续提交文件。", code="unsafe_path")
    actual_signature = hashlib.sha256(state_path.read_bytes()).hexdigest() if state_path.exists() else None
    if actual_signature != signature:
        raise SyncError("同步记录在提交前发生变化，未继续改动文件。", code="state_conflict")
    if state["course_id"] not in (None, entry["course_id"]):
        raise SyncError("恢复记录与课程不匹配，未改动文件。", code="recovery_conflict")
    already_recorded = state["files"].get(identity) == entry
    if signature != journal["signature"] and not already_recorded:
        raise SyncError("恢复记录与当前同步记录不一致，未改动文件。", code="recovery_conflict")
    target = _target(root, entry["local_path"])
    temporary = _target(root, journal["temporary"])
    backup = _target(root, journal["backup"]) if journal["backup"] else None
    target_new = _matches(target, entry)
    if not target_new:
        if not _matches(temporary, entry):
            raise SyncError("恢复所需的新文件缺失或已变化，已保留现有文件。", code="recovery_conflict")
        if _exists(target):
            if previous is None or not _matches(target, previous) or backup is None or _exists(backup):
                raise SyncError("恢复目标已被其他内容占用，未覆盖文件。", code="recovery_conflict")
            _move_no_replace(target, backup)
        elif previous is not None and (backup is None or not _matches(backup, previous)):
            raise SyncError("恢复所需的旧版本缺失或已变化，已停止。", code="recovery_conflict")
        try:
            _move_no_replace(temporary, target)
        except Exception:
            if backup is not None and _exists(backup) and not _exists(target):
                try:
                    _move_no_replace(backup, target)
                except Exception:
                    pass
            raise
    if not already_recorded:
        state["course_id"] = entry["course_id"]
        state["version"] = max(STATE_VERSION, state["version"])
        state["files"][identity] = entry
        signature = _write_state(root, state, secrets, signature, state_name)
    # Only remove a generated, fully verified duplicate temporary file.
    if _exists(temporary) and _matches(temporary, entry):
        temporary.unlink()
    _journal_path(root, state_name).unlink()
    return signature


def _discard_uncommitted_journal(root, state_name, secrets, hosts):
    """An ordinary rename failure can finish rollback and keep the safe .part.

    A crash never enters this path. A new target already on disk, a changed state,
    or any uncertain old-version location always keeps the recovery journal.
    """
    journal = _read_journal(root, state_name, secrets, hosts)
    if journal is None:
        return True
    state_path = root / state_name
    signature = hashlib.sha256(state_path.read_bytes()).hexdigest() if state_path.exists() else None
    if signature != journal["signature"]:
        return False
    target = _target(root, journal["entry"]["local_path"])
    previous = journal["previous"]
    if ((previous is None and _exists(target)) or
            (previous is not None and not _matches(target, previous)) or
            (journal["backup"] is not None and _exists(_target(root, journal["backup"])) ) or
            not _matches(_target(root, journal["temporary"]), journal["entry"])):
        return False
    _journal_path(root, state_name).unlink()
    return True


def _recover_directory(root, secrets, dry_run):
    """Recover shared-directory transactions before another course picks names."""
    if not root.exists():
        return
    if (root / ".entrepreneurship-reorganize.json").exists():
        if dry_run:
            raise SyncError("资料整理尚未完成，请先恢复整理；预览未改动文件。", code="recovery_pending")
        spec = importlib.util.spec_from_file_location(
            "_tum_reorganize", Path(__file__).with_name("material_reorganize.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.recover(root)
    pending = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        suffix = ".journal.json"
        if not path.name.endswith(suffix):
            continue
        state_name = path.name[:-len(suffix)]
        if not _STATE_FILE.fullmatch(state_name):
            continue
        hosts = frozenset({"artemis.tum.de"}) if state_name.lower().startswith(".artemis-") else DEFAULT_HOSTS
        journal = _read_journal(root, state_name, secrets, hosts)
        state, signature = _read_state(root, secrets, hosts, state_name)
        pending.append((state_name, journal, state, signature))
    if pending and dry_run:
        raise SyncError("上次同步中断，需先运行同步恢复记录；预览未改动文件。", code="recovery_pending")
    # Validate every referenced state/journal before any recovery changes files.
    for state_name, journal, state, signature in pending:
        try:
            _finish_commit(root, state, signature, state_name, journal, secrets)
        except SyncError:
            raise
        except Exception:
            raise SyncError("上次同步恢复尚未完成，文件与恢复记录均已保留。",
                            code="recovery_pending", retryable=True) from None


def _course_id(course, activities, client, state):
    ids = {_field(activity, "course_id") for activity in activities}
    ids.discard(None)
    if len(ids) > 1 or any(type(value) is not int or value <= 0 for value in ids):
        raise SyncError("课程活动的编号不一致，已停止同步。")
    if ids:
        return ids.pop()
    value = _field(course, "id", course)
    if str(value).isdigit() and int(value) > 0:
        return int(value)
    # activities() has already populated TumMoodle's course cache; no new request.
    cached = getattr(client, "_courses_cache", None) or []
    key = str(course).strip().casefold()
    exact = [item for item in cached if str(_field(item, "id")) == key or
             str(_field(item, "shortname", "")).casefold() == key]
    matches = exact or [item for item in cached if key in str(_field(item, "shortname", "")).casefold()
                       or key in str(_field(item, "name", "")).casefold()]
    if len(matches) == 1:
        return int(_field(matches[0], "id"))
    if state["files"]:
        raise SyncError("课程为空且无法确认课程编号；为保护旧文件，未判断线上删除。")
    return None


def _snapshot(client, activity, secrets, hosts):
    hook = getattr(client, "sync_text", None)
    if not callable(hook) or _field(activity, "type") not in ("page", "label"):
        return None
    data = hook(activity)
    if data is None:
        return None
    if (not isinstance(data, dict) or set(data) != {"text", "filename", "url"}
            or any(not isinstance(data[key], str) for key in data)
            or not data["filename"].strip()):
        raise SyncError("文字快照格式不认识，未保存。", code="invalid_snapshot")
    url = _safe_url(data["url"], secrets | _secrets(client), hosts)
    _check_secrets(data, secrets | _secrets(client))
    if re.search(r"(?:sesskey|moodlesession|access_token|jwt)\s*=", unquote(data["text"]), re.I):
        raise SyncError("文字快照含会话字段，未保存。", code="unsafe_snapshot")
    filename = data["filename"]
    if not filename.lower().endswith((".md", ".txt")):
        filename += ".md"
    return {"name": filename, "url": url, "_snapshot_text": data["text"]}


def _error_details(exc):
    if _authentication_failed(exc):
        return "登录已失效或被拒绝，已停止本课程的后续请求；请确认账号后重试。", "auth_failed", False
    if isinstance(exc, SyncError):
        return str(exc), exc.code, exc.retryable
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        return "文件访问已失效或没有权限，请确认登录和课程权限。", "authentication" if status == 401 else "forbidden", False
    if status == 429:
        return "网站暂时限制访问频率，请稍后重试。", "rate_limited", True
    if isinstance(status, int) and status >= 500:
        return "网站暂时无法提供文件，请稍后重试。", "server_error", True
    if "timeout" in type(exc).__name__.lower() or "connection" in type(exc).__name__.lower():
        return "网络连接未完成，请稍后重试。", "network_error", True
    return "此文件同步失败；已保留原文件及未完成的临时文件。", "sync_failed", False


def _artifacts(root, state, secrets):
    artifacts = []
    for identity, entry in state["files"].items():
        if entry.get("sha256") is None:
            continue
        try:
            if not _matches(_target(root, entry["local_path"]), entry):
                continue
            history = [item for item in entry["history"]
                       if _matches(_target(root, item["local_path"]), item)]
            item = {key: entry[key] for key in ("course_id", "cmid", "filename", "url", "local_path", "sha256", "size")}
            item.update(identity=identity, version=entry["sha256"], history=history)
            _check_secrets(item, secrets)
            artifacts.append(item)
        except (SyncError, OSError):
            continue
    return artifacts


def _mirror_cleanup(root, state, removed, report):
    """After every file is committed: old versions and files removed online go to the
    Recycle Bin and leave the state. A file another entry still uses as its current
    file is only forgotten; a removed file the user changed is kept, untracked."""
    removed = set(removed)
    in_use = {entry["local_path"].casefold() for identity, entry in state["files"].items()
              if identity not in removed}

    def discard(relative, expected):
        """True when the file is gone (trashed or already missing); False keeps the record."""
        target = _target(root, relative)
        if not _exists(target):
            return True
        if not _matches(target, expected):
            return None  # Changed by the user: not ours to discard.
        try:
            _trash(target)
        except Exception as exc:
            message, code, retryable = _error_details(exc)
            report("failed", relative, message, code, retryable)
            return False
        report("trashed", relative)
        return True

    for identity, entry in state["files"].items():
        kept = []
        for previous in entry["history"]:
            if previous["local_path"].casefold() not in in_use and discard(previous["local_path"], previous) is False:
                kept.append(previous)
        entry["history"] = kept
    for identity in sorted(removed):
        entry = state["files"][identity]
        relative = entry["local_path"]
        if relative.casefold() not in in_use:
            outcome = discard(relative, entry) if entry.get("sha256") is not None else None
            if outcome is False:
                continue
            if outcome is None and _exists(_target(root, relative)):
                report("kept_local", relative)
        del state["files"][identity]


def _sync_course(client, course, dest_dir, dry_run=True, state_name=None) -> dict:
    """Preview or mirror available resource/folder files; nothing is deleted for good.

    Each result category is ``{count: int, files: [relative path, ...]}``.
    Per-file errors contain fixed safe explanations, never raw request exceptions.
    Unknown/corrupt state is rejected before changing any local files.
    """
    if type(dry_run) is not bool:
        raise SyncError("dry_run 必须是 true 或 false。")
    try:
        root = Path(dest_dir).expanduser().absolute()
    except (TypeError, ValueError, OSError):
        raise SyncError("同步目录无效。") from None
    if _is_link(root) or (_exists(root) and not root.is_dir()):
        raise SyncError("同步目录必须是普通目录，不能是文件或链接。")
    if _units(str(root)) > MAX_PATH_UNITS - 25:
        raise SyncError("同步目录过长，请选择更短的目录。")
    secrets = _secrets(client)
    _check_secrets(str(root), secrets)
    hosts = frozenset(getattr(client, "sync_hosts", DEFAULT_HOSTS))
    state_name = state_name or getattr(client, "sync_state_name", STATE_NAME)
    if not isinstance(state_name, str) or not _STATE_FILE.fullmatch(state_name):
        raise SyncError("同步状态文件名不受支持。")
    by_kind = getattr(client, "sync_layout", None) == "kind"
    _read_state(root, secrets, hosts, state_name)  # Reject a corrupt current state before recovery.
    _recover_directory(root, secrets, dry_run)
    state, signature = _read_state(root, secrets, hosts, state_name)
    activities = list(client.activities(course))
    course_id = _course_id(course, activities, client, state)
    if state["course_id"] not in (None, course_id):
        raise SyncError("此目录已用于另一门课程，请为当前课程选择其他目录。")
    state["course_id"] = course_id
    profiled = by_kind and course_id in entrepreneurship.COURSES and hosts == DEFAULT_HOSTS
    relayout = by_kind and course_id in ACTIVITY_NAMED_COURSES and hosts == DEFAULT_HOSTS
    if profiled:
        state["version"] = 3
        activities.sort(key=lambda a: _field(a, "id", 0))
    results = {"dry_run": dry_run, "course_id": course_id,
               **{kind: {"count": 0, "files": []} for kind in KINDS}, "errors": [],
               "artifacts": [], "coverage": []}
    reserved = {entry["local_path"].casefold(): key for key, entry in state["files"].items()}
    current = set()
    protected_cmids = set()
    now = datetime.now(timezone.utc).isoformat()
    auth_failed = False

    def report(kind, path, error=None, code="sync_failed", retryable=False):
        # Path strings can be influenced by server labels; reject loaded secrets too.
        try:
            _check_secrets(path, secrets | _secrets(client))
        except SyncError:
            path = "[文件路径已隐藏]"
        results[kind]["files"].append(path)
        results[kind]["count"] += 1
        if error:
            results["errors"].append({"path": path, "error": error, "code": code, "retryable": retryable})

    for activity in activities:
        cmid = _field(activity, "id")
        activity_type = _field(activity, "type")
        coverage = {"cmid": cmid, "type": activity_type, "status": "checked", "files": 0}
        try:
            _check_secrets(coverage, secrets | _secrets(client))
        except SyncError:
            coverage = {"cmid": None, "type": "hidden", "status": "failed", "files": 0}
        results["coverage"].append(coverage)
        if auth_failed:
            coverage["status"] = "auth_failed"
            protected_cmids.add(cmid)
            report("failed", "[未读取的活动]", "登录失败后已停止后续请求。", "auth_failed")
            continue
        if not _field(activity, "available", False):
            coverage["status"] = "unavailable"
            protected_cmids.add(cmid)
            continue
        if activity_type not in ("resource", "folder", "page", "label"):
            coverage["status"] = "unsupported"
            protected_cmids.add(cmid)
            continue
        fallback = safe_name(_field(activity, "section", "") or "General") + "/" + safe_name(_field(activity, "name", "") or "Activity")
        if type(cmid) is not int or cmid <= 0 or course_id is None:
            report("failed", fallback, "活动编号无效，未同步该活动。")
            coverage["status"] = "auth_failed" if auth_failed else "failed"
            protected_cmids.add(cmid)
            continue
        try:
            files = list(client.files(activity))
            if not files and activity_type == "resource" and not _field(activity, "url"):
                coverage["status"] = "external"
        except Exception as exc:
            auth_failed = _authentication_failed(exc)
            if auth_failed:
                message, code, retryable = _error_details(exc)
                report("failed", fallback, message, code, retryable)
            else:
                report("failed", fallback, "无法读取此活动的文件清单；原文件和同步记录均已保留。", "listing_failed")
            protected_cmids.add(cmid)
            coverage["status"] = "auth_failed" if auth_failed else "failed"
            continue
        try:
            snapshot = _snapshot(client, activity, secrets, hosts)
            if snapshot is not None:
                files.append(snapshot)
        except Exception as exc:
            auth_failed = _authentication_failed(exc)
            message, code, retryable = _error_details(exc)
            report("failed", fallback, message, code, retryable)
            coverage["status"] = "auth_failed" if auth_failed else "partial"
            protected_cmids.add(cmid)
        coverage["files"] = len(files)
        seen_in_activity = set()
        for file in files:
            relative = fallback + "/" + safe_name(_field(file, "name", "") or "File")
            identity = None
            if auth_failed:
                protected_cmids.add(cmid)
                report("failed", relative, "登录失败后已停止后续请求。", "auth_failed")
                continue
            try:
                secrets.update(_secrets(client))
                filename = _field(file, "name")
                if not isinstance(filename, str) or not filename:
                    raise SyncError("文件名称为空，未下载该文件。")
                url = _safe_url(_field(file, "url"), secrets, hosts)
                snapshot_text = _field(file, "_snapshot_text")
                folder_parts = _folder_parts(activity, file, url)
                folder_path = "@text" if snapshot_text is not None else "/".join(folder_parts)
                identity = _identity(course_id, cmid, folder_path, filename)
                current.add(identity)
                if identity in seen_in_activity:
                    continue
                seen_in_activity.add(identity)
                old = state["files"].get(identity)
                if old is not None:
                    relative = old["local_path"]
                    target = _target(root, relative)
                    if _exists(target) and not target.is_file():
                        raise SyncError("本地目标不是普通文件，已拒绝替换。")
                    if relayout and snapshot_text is None:
                        # Follow a changed naming rule: move an unmodified file to its new name.
                        desired = _layout(root, activity, folder_parts, filename, by_kind)
                        moved, arrived = _relocation(root, desired, reserved, identity, old)
                        unmodified = not _exists(target) or (old.get("sha256") is not None and _matches(target, old))
                        if moved != relative and unmodified and not (arrived and _exists(target)):
                            if not dry_run:
                                if _exists(target):
                                    _target(root, moved).parent.mkdir(parents=True, exist_ok=True)
                                    _move_no_replace(target, _target(root, moved))
                                old["local_path"] = moved
                                signature = _write_state(root, state, secrets | _secrets(client), signature, state_name)
                            if reserved.get(relative.casefold()) == identity:
                                del reserved[relative.casefold()]
                            report("renamed", f"{relative} → {moved}")
                            relative, target = moved, _target(root, moved)
                else:
                    relative = (_layout(root, {"kind": "Other"}, [], filename, True)
                                if snapshot_text is not None else
                                _layout(root, activity, folder_parts, filename, by_kind))
                    relative = _unique(root, relative, reserved, identity)
                    target = _target(root, relative)
                reserved[relative.casefold()] = identity
                _check_secrets([identity, relative, folder_path, filename], secrets)
                local = _fingerprint(target) if old is not None else None
                modified = (old is not None and local is not None and old.get("sha256") is not None
                            and local != {key: old[key] for key in ("sha256", "size")})
                if old is not None and old.get("sha256") is None and local is not None:
                    old.update(local)
                metadata = ({"etag": None, "last_modified": None} if snapshot_text is not None else
                            _metadata(client, url, secrets, filename, hosts))
                stored = None if old is None else {**old, "etag": _plain_etag(old["etag"])}
                changed = old is not None and any(stored[key] != value for key, value in
                                                 {"url": url, **metadata}.items())
                needs_body = snapshot_text is not None or not any(metadata.values())
                replaced = False
                if modified:
                    if not changed or needs_body or profiled:
                        report("local_modified", relative, "本地文件与上次同步内容不同，已保留；请先检查本地修改。", "local_modified")
                        coverage["status"] = "local_modified"
                        continue
                    # Both changed: keep the user's copy beside the new version, untracked.
                    kept = _annotated_relative(root, relative, reserved)
                    reserved[kept.casefold()] = "kept:" + identity
                    if not dry_run:
                        _move_no_replace(target, _target(root, kept))
                    report("kept_local", kept)
                    local, replaced = None, True
                exists = _exists(target) and not replaced
                if old is not None and exists and not changed and not needs_body:
                    kind = "unchanged"
                elif old is not None and exists:
                    kind = "updated"
                else:
                    kind = "new"
                backup_relative = None
                if kind == "updated":
                    backup_relative = _backup_relative(root, relative, reserved)
                    # Preview the backup too, so subsequent new names match the
                    # real run even when a future file is literally named *_old.
                    reserved[backup_relative.casefold()] = "backup:" + identity
                if dry_run and needs_body and old is not None and exists:
                    # Preview may inspect a body but never stage it on disk.
                    if snapshot_text is not None:
                        payload = snapshot_text.encode("utf-8")
                        observed = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                    else:
                        response = None
                        try:
                            response = _request(client, "GET", url, stream=True, allow_redirects=True,
                                                timeout=getattr(client, "timeout", 30))
                            _check_response(response, url, secrets | _secrets(client), filename, hosts)
                            digest, size = hashlib.sha256(), 0
                            for chunk in safe_download_chunks(response, client, secrets):
                                digest.update(chunk)
                                size += len(chunk)
                            observed = {"sha256": digest.hexdigest(), "size": size}
                        finally:
                            _close(response)
                    kind = "unchanged" if observed == local else "updated"
                if not dry_run:
                    temporary = None
                    content = local
                    previous_for_commit = old if local is not None else None
                    if kind != "unchanged" or needs_body:
                        metadata, temporary, content = _download(client, root, relative, url, metadata,
                                                                 secrets, hosts, snapshot_text)
                        if local is not None and content == local:
                            kind = "unchanged"
                            _target(root, temporary).unlink()
                            temporary = None
                        elif local is not None:
                            kind = "updated"
                            if backup_relative is None:
                                backup_relative = _backup_relative(root, relative, reserved)
                                reserved[backup_relative.casefold()] = "backup:" + identity
                        else:
                            kind = "new"
                    history = list(old["history"]) if old is not None else []
                    alias_of = old.get("alias_of") if old else None
                    if temporary is not None and profiled:
                        # Reading duplicates retain separate source identities/metadata.
                        # A shared current file is immutable until its sources split.
                        peer = next(((key, row) for key, row in state["files"].items()
                                     if key != identity and row["cmid"] in entrepreneurship.READINGS
                                     and cmid in entrepreneurship.READINGS
                                     and row["filename"] == filename and row["sha256"] == content["sha256"]
                                     and row["size"] == content["size"]
                                     and _matches(_target(root, row["local_path"]), row)), None)
                        shared = [row for key, row in state["files"].items()
                                  if key != identity and row["local_path"].casefold() == relative.casefold()]
                        if peer is not None:
                            if local is not None and local != content:
                                history.append({**local, "local_path": relative})
                            _target(root, temporary).unlink()
                            temporary = None
                            relative = peer[1]["local_path"]
                            alias_of = peer[1].get("alias_of", peer[0])
                            backup_relative = None
                        elif shared and content != {key: old[key] for key in ("sha256", "size")}:
                            if local is not None:
                                history.append({**local, "local_path": relative})
                            desired = _layout(root, activity, folder_parts, filename, by_kind)
                            occupied = {**reserved, relative.casefold(): "shared:" + identity}
                            relative = _unique(root, desired, occupied, identity)
                            target = _target(root, relative)
                            relocated = target.with_name(_shorten(target.name, MAX_COMPONENT_UNITS - 14)
                                                         + "." + uuid4().hex[:8] + ".part")
                            _move_no_replace(_target(root, temporary), relocated)
                            temporary = relocated.relative_to(root).as_posix()
                            reserved[relative.casefold()] = identity
                            previous_for_commit = None
                            backup_relative = None
                            alias_of = None
                    if temporary is not None and kind == "updated" and backup_relative is not None:
                        history.append({**local, "local_path": backup_relative})
                    entry = {"course_id": course_id, "cmid": cmid,
                             "folder_path": folder_path, "filename": filename, "url": url,
                             **metadata, "local_path": relative, "last_sync": now,
                             **content, "history": history}
                    if alias_of is not None:
                        entry["alias_of"] = alias_of
                    _check_secrets(entry, secrets | _secrets(client))
                    if temporary is not None:
                        transaction = {"version": 1, "identity": identity, "entry": entry,
                                       "previous": previous_for_commit,
                                       "temporary": temporary,
                                       "backup": backup_relative if kind == "updated" else None,
                                       "signature": signature}
                        _save_journal(root, state_name, transaction, secrets | _secrets(client))
                        signature = _finish_commit(root, state, signature, state_name, transaction,
                                                   secrets | _secrets(client))
                    else:
                        state["files"][identity] = entry
                        signature = _write_state(root, state, secrets | _secrets(client), signature, state_name)
                report("updated" if replaced and kind == "new" else kind, relative)
            except Exception as exc:
                if _authentication_failed(exc):
                    auth_failed = True
                    protected_cmids.add(cmid)
                if _journal_path(root, state_name).exists():
                    try:
                        safely_rolled_back = _discard_uncommitted_journal(
                            root, state_name, secrets | _secrets(client), hosts)
                    except Exception:
                        safely_rolled_back = False
                    if not safely_rolled_back:
                        raise SyncError("同步提交尚未完成，文件与恢复记录已保留；再次同步会先尝试恢复。",
                                        code="recovery_pending", retryable=True) from None
                if identity is None:
                    # Malformed file identities cannot establish that old files vanished.
                    protected_cmids.add(cmid)
                message, code, retryable = _error_details(exc)
                report("failed", relative, message, code, retryable)
                coverage["status"] = "auth_failed" if auth_failed else "partial"

    if not activities:
        # An empty course page is more likely a glitch than every file deleted at once.
        protected_cmids.update(entry["cmid"] for entry in state["files"].values())
    removed = [identity for identity, entry in state["files"].items()
               if identity not in current and entry["cmid"] not in protected_cmids]
    for identity in removed:
        report("removed_online", state["files"][identity]["local_path"])
    if not dry_run:
        try:
            _mirror_cleanup(root, state, removed, report)
        except SyncError:
            raise
        except Exception:
            raise SyncError("清理旧版本时出错；所有本地文件和同步记录均已保留。") from None
        try:
            _write_state(root, state, secrets | _secrets(client), signature, state_name)
        except SyncError:
            raise
        except Exception:
            raise SyncError("文件处理已结束，但无法保存同步记录；所有本地文件均已保留。") from None
    results["artifacts"] = _artifacts(root, state, secrets | _secrets(client))
    _check_secrets(results, secrets | _secrets(client))
    return results


def sync_course(client, course, dest_dir, dry_run=True, state_name=None) -> dict:
    """Preview by default; ``dry_run=False`` mirrors the online files and writes sync state.

    Result categories: new/updated/unchanged/removed_online/failed/local_modified,
    trashed (old versions and removed files moved to the Recycle Bin), kept_local
    (user-changed copies kept as "<name> (批注)" or left untracked) and renamed
    ("old → new" after a naming rule change), each containing count and files
    (relative paths). Different threads in this process are serialized.
    A directory mutex also serializes independent processes using one destination.
    ``state_name`` (e.g. ".moodle-sync-123.json") lets courses share one directory.
    """
    with _SYNC_LOCK:
        try:
            # Windows named mutexes have no disk side effects, including previews.
            if os.name == "nt" or not dry_run:
                with directory_lock(dest_dir):
                    return _sync_course(client, course, dest_dir, dry_run, state_name)
            return _sync_course(client, course, dest_dir, dry_run, state_name)
        except DirectoryBusyError:
            raise SyncError("此目录正在由另一进程同步，请稍后重试。", code="directory_busy", retryable=True) from None
