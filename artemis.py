"""Read-only Artemis (https://artemis.tum.de) courses and lecture files.

Artemis checks the TUM ID and password itself (LDAP) and answers with an HttpOnly
``jwt`` cookie; there is no SSO hop and no 2FA step. The credentials come from the
caller (the same Windows Credential Manager entry as Moodle); the cookie only
lives in this process's memory. Requests only go to artemis.tum.de.

Endpoints (Artemis 10.3, checked live 2026-10-20 with a student account):
POST /api/core/public/authenticate, GET /api/course/courses/for-dashboard,
GET /api/lecture/courses/<id>/lectures-with-slides, and student file downloads
under /api/core/files/attachments/.../student/<name>. Exercises (checked live
2026-10-22): GET /api/course/courses/<id>/exercises-for-overview. Course channels
and paginated posts/replies (10.6, checked live 2026-11-09): GET
/api/communication/courses/<id>/channels/overview and /messages. Always send
searchText (even empty) on message reads to avoid changing the unread state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from itertools import chain
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit
from uuid import uuid4

import requests
from moodlekit.client import MoodleError, NotLoggedIn
from moodlekit.utils import Utils

import moodle_sync

ARTEMIS_URL = "https://artemis.tum.de"
ARTEMIS_HOST = "artemis.tum.de"
LOGIN_PATH = "/api/core/public/authenticate"
FILES_PATH = "/api/core/files/"
STATE_NAME = ".artemis-sync.json"  # Default only; sync_course names it per course.
COURSE_CACHE_TTL = 600
TRANSFER_RETRIES = 2
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class CredentialsRejected(NotLoggedIn):
    """A definite password refusal; automatic attempts stop until process restart."""


def transfer_retry_delay(response, attempt: int) -> float:
    """Bound Retry-After, including HTTP dates, to two seconds per retry."""
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 0.25 * (2 ** attempt)
    return max(0.0, min(2.0, delay))


def buffered_login_page(response) -> bool:
    """Inspect a bounded stream prefix in memory and replay every byte to its owner."""
    chunks = iter(response.iter_content(1 << 16))
    buffered = []
    size = 0
    while size < 1 << 16:
        chunk = next(chunks, None)
        if chunk is None:
            break
        if not isinstance(chunk, bytes):
            raise MoodleError("下载响应格式不正确，未保存该内容。")
        buffered.append(chunk)
        size += len(chunk)
    replay = chain(buffered, chunks)
    original = response.iter_content

    def replay_content(chunk_size=1, decode_unicode=False):
        response.iter_content = original
        return replay

    response.iter_content = replay_content
    prefix = b"".join(buffered)[:1 << 16].lower()
    return b"<form" in prefix and bool(
        b"/login/index.php" in prefix
        or re.search(br"name\s*=\s*['\"]?(?:j_password|logintoken)\b", prefix)
        or re.search(br"(?:id|class)\s*=\s*['\"][^'\"]*\blogin[-_ ]?form\b", prefix))


def artemis_url(url: str) -> str:
    """Validate before any request; never include the rejected URL in an error."""
    error = "URL 必须使用 https，且主机必须为 artemis.tum.de。"
    if (not isinstance(url, str) or not url or "\\" in url
            or any(c.isspace() or ord(c) < 32 for c in url)):
        raise MoodleError(error)
    if url.startswith("/") and not url.startswith("//"):
        url = urljoin(ARTEMIS_URL, url)
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname == ARTEMIS_HOST
                 and parts.username is None and parts.password is None
                 and parts.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise MoodleError(error)
    return url


# Files posted in a course channel (checked live 2026-11-15, the SES1 instructions PDF):
# /api/core/files/courses/<course>/conversations/<channel>/<upload time>_<file name>.
_CONVERSATION_FILE = re.compile(r"/api/core/files/courses/\d+/conversations/\d+/[^/]+")
_POST_FILE = re.compile(r"(!?)\[([^\]\n]*)\]\((/api/core/files/courses/(\d+)/conversations/\d+/[^)\s]+)\)")


def file_url(url: str) -> str:
    """Only lecture attachments and course-channel files, never other API endpoints."""
    url = artemis_url(url)
    path = unquote(urlsplit(url).path)
    if (not (path.startswith(FILES_PATH + "attachments/") or _CONVERSATION_FILE.fullmatch(path))
            or "/../" in path + "/" or "/./" in path + "/"):
        raise MoodleError("只能下载 Artemis 讲座附件（/api/core/files/attachments/...）"
                          "或课程频道里的文件（/api/core/files/courses/<课程>/conversations/<频道>/...）。")
    return url


def post_attachments(content, course_id: int) -> list[dict]:
    """Files linked in a post's Markdown, as [name](/api/core/files/courses/...) or ![image](...)."""
    found, seen = [], set()
    for image, name, path, course in _POST_FILE.findall(content or ""):
        url = ARTEMIS_URL + path
        if int(course) != course_id or url in seen:
            continue
        try:
            file_url(url)
        except MoodleError:
            continue
        seen.add(url)
        found.append({"name": " ".join(name.split()) or unquote(path.rsplit("/", 1)[-1]),
                      "url": url, "image": bool(image)})
    return found


def student_file_url(link) -> str | None:
    """The download URL a student may use for an attachment link, as the web app builds it.

    Unit links look like attachments/attachment-video-units/<unit>/<file>; students
    download them from .../<unit>/student/<file> (the plain path needs tutor rights).
    Lecture-level links (attachments/lecture/...) are used unchanged. External
    links (http...) are not Artemis files and give None.
    """
    if not isinstance(link, str) or not link.strip() or "://" in link:
        return None
    path = unquote(link.strip()).lstrip("/")
    for prefix in ("api/core/files/", "api/files/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    parts = path.split("/")
    if not path.startswith("attachments/") or any(p in ("", ".", "..") for p in parts):
        return None
    if not path.startswith("attachments/lecture/") and "student" not in parts[:-1]:
        head, _, name = path.rpartition("/")
        path = f"{head}/student/{name}"
    return ARTEMIS_URL + FILES_PATH + quote(path, safe="/")


def attachment_filename(attachment: dict) -> str:
    """The attachment's display name plus the stored file's extension."""
    link = str(attachment.get("link") or "")
    suffix = PurePosixPath(urlsplit(link).path).suffix if "://" not in link else ""
    name = " ".join(str(attachment.get("name") or "").split()) or "Attachment"
    if suffix and not name.lower().endswith(suffix.lower()):
        name += suffix
    return name


def readable_name(text: str) -> str:
    """Turn separators Windows cannot store into dashes: "a / b" -> "a - b", "WS2025/26" -> "WS2025-26"."""
    text = re.sub(r"\s+[/\\|:]\s+|\s*:\s+", " - ", str(text or ""))
    text = re.sub(r"[/\\|:]", "-", text)
    return " ".join(text.split())


# Lecture titles that only name a group of files; they add nothing to a file name.
_GROUP_TITLES = {"past exams", "exams", "old exams", "altklausuren", "organisation", "organization", "general",
                 "allgemeines", "material", "materials", "other", "others", "slides", "tutorials", "exercises"}
_UPLOADED = re.compile(r"AttachmentUnit_(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})")


def material_kind(lecture: dict, attachment: dict) -> str:
    """The sync folder (moodle_sync.KIND_FOLDERS); unknown lecture units count as Slides."""
    texts = [attachment.get("name"), lecture.get("title")]
    if lecture.get("is_tutorial"):
        return "Exams" if moodle_sync.material_kind(texts) == "Exams" else "Exercises"
    return moodle_sync.material_kind(texts, default="Slides")


def material_layout(lectures: list[dict]) -> list[dict]:
    """Where each attachment goes when syncing: {kind}/{lecture title - attachment name}.ext.

    The lecture title is left out when it is only a group name ("Past Exams") or
    already part of the attachment name. Equal names in one folder are versions of
    one file (instructors re-upload into a new lecture): only the newest upload is
    kept. Its sync id comes from the folder and name, not the Artemis unit, so a
    later re-upload counts as an update of the same local file (old copy -> *_old).
    """
    files = []
    for lecture in lectures:
        title = readable_name(lecture.get("title"))
        for attachment in lecture.get("attachments", []):
            name = readable_name(attachment.get("name")) or "Attachment"
            if title.casefold() in _GROUP_TITLES or title.casefold() in name.casefold():
                base = name
            elif name.casefold() in title.casefold():
                base = title
            else:
                base = f"{title} - {name}"
            suffix = PurePosixPath(attachment.get("filename") or "").suffix
            if suffix and base.lower().endswith(suffix.lower()):
                base = base[:-len(suffix)]
            files.append({"unit_id": attachment["unit_id"], "kind": material_kind(lecture, attachment),
                          "base": base, "suffix": suffix, "url": attachment.get("url"),
                          "uploaded": attachment.get("uploaded"), "course_id": lecture.get("course_id")})
    newest = {}
    for item in files:
        key = (item["kind"], (item["base"] + item["suffix"]).casefold())
        current = newest.get(key)
        if current is None or (item["uploaded"] or "", item["unit_id"]) > (current["uploaded"] or "", current["unit_id"]):
            newest[key] = item
    result = []
    for item in files:
        key = (item["kind"], (item["base"] + item["suffix"]).casefold())
        if newest[key] is not item:
            continue
        item["filename"] = item.pop("base") + item.pop("suffix")
        digest = hashlib.sha256(f"{key[0]}/{key[1]}".encode("utf-8")).hexdigest()
        item["sync_id"] = int(digest[:12], 16) or 1
        result.append(item)
    return result


def parse_courses(data) -> list[dict]:
    """Courses from /api/course/courses/for-dashboard ({courses: [{course: {...}}]})."""
    entries = data.get("courses") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise MoodleError("Artemis 课程列表格式不认识；接口可能已变化。")
    courses = []
    for entry in entries:
        course = entry.get("course", entry) if isinstance(entry, dict) else None
        if not isinstance(course, dict) or type(course.get("id")) is not int:
            continue
        _string_fields(course, ("title", "shortName", "semester", "startDate", "endDate"), "课程")
        courses.append({"id": course["id"], "title": course.get("title") or "",
                        "short_name": course.get("shortName") or "",
                        "semester": course.get("semester"),
                        "start_date": course.get("startDate"), "end_date": course.get("endDate")})
    if entries and not courses:
        raise MoodleError("Artemis 课程列表没有可识别的课程；接口可能已变化。")
    return courses


def _string_fields(data, fields, kind):
    if any(data.get(key) is not None and not isinstance(data[key], str) for key in fields):
        raise MoodleError(f"Artemis {kind}字段格式不认识；接口可能已变化。")


def parse_lectures(data, course_id: int) -> list[dict]:
    """Lectures and their attachments from .../lectures-with-slides, by start date.

    The endpoint only returns attachment units visible to students. Units without
    a file (e.g. a video link only) are omitted; external links keep url=None.
    """
    if not isinstance(data, list):
        raise MoodleError("Artemis 讲座列表格式不认识；接口可能已变化。")
    lectures = []
    for lecture in data:
        if not isinstance(lecture, dict) or type(lecture.get("id")) is not int:
            raise MoodleError("Artemis 讲座缺少有效编号；接口可能已变化。")
        _string_fields(lecture, ("title", "description", "startDate", "endDate"), "讲座")
        # A lecture that has no content yet (2026-10-25, FoP) comes without the key.
        units = lecture.get("lectureUnits")
        if units is None:
            units = []
        if not isinstance(units, list):
            raise MoodleError("Artemis 讲座单元列表格式不认识；接口可能已变化。")
        attachments = []
        for unit in units:
            if not isinstance(unit, dict) or type(unit.get("id")) is not int:
                raise MoodleError("Artemis 讲座单元格式不认识；接口可能已变化。")
            attachment = unit.get("attachment") if isinstance(unit, dict) else None
            if attachment is not None and not isinstance(attachment, dict):
                raise MoodleError("Artemis 附件格式不认识；接口可能已变化。")
            if isinstance(attachment, dict) and "link" in attachment and not isinstance(attachment["link"], str):
                raise MoodleError("Artemis 附件链接格式不认识；接口可能已变化。")
            if isinstance(attachment, dict):
                _string_fields(attachment, ("name", "releaseDate"), "附件")
            if not isinstance(attachment, dict) or not attachment.get("link") or type(unit.get("id")) is not int:
                continue
            link = str(attachment["link"])
            url = student_file_url(link)
            uploaded = _UPLOADED.search(link)
            attachments.append({
                "unit_id": unit["id"], "name": attachment.get("name") or unit.get("name") or "",
                "filename": attachment_filename(attachment),
                "release_date": attachment.get("releaseDate") or unit.get("releaseDate"),
                "uploaded": f"{uploaded[1]} {uploaded[2]}-{uploaded[3]}" if uploaded else None,
                "url": url,
                "external_url": link if url is None and link.startswith("https://") else None,
            })
        lectures.append({"id": lecture["id"], "title": lecture.get("title") or "",
                         "start_date": lecture.get("startDate"), "end_date": lecture.get("endDate"),
                         "is_tutorial": bool(lecture.get("isTutorialLecture")),
                         "description": lecture.get("description"), "course_id": course_id,
                         "attachments": attachments})
    return sorted(lectures, key=lambda l: (l["start_date"] is None, l["start_date"] or "", l["id"]))


_COUNTS = {"INCLUDED_COMPLETELY": "graded", "INCLUDED_AS_BONUS": "bonus", "NOT_INCLUDED": "not_graded"}


def _when(value):
    """An Artemis timestamp ("...Z") as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _categories(values) -> list[str]:
    """Category names; Artemis sends each as a JSON string like '{"category": "Homework", ...}'."""
    names = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                names.append(value)
                continue
        if isinstance(value, dict) and value.get("category"):
            names.append(str(value["category"]))
    return names


def _best(scores) -> float | None:
    return max(scores) if scores else None


def parse_exercises(data, course_id: int, now: datetime | None = None) -> list[dict]:
    """A student's exercises from .../exercises-for-overview, by due date (none last).

    status (against now): not_released, open, closed. participation: not_started
    (no participation), started (no result yet) or result. score is the best rated
    result of a graded participation in percent; practice runs (testRun) count
    separately as practice_score. The endpoint has no submission details.
    """
    if not isinstance(data, dict) or not isinstance(data.get("exercises", []), list):
        raise MoodleError("Artemis 练习列表格式不认识；接口可能已变化。")
    if any(key in data for key in ("error", "errorKey", "exception", "statusCode")):
        raise MoodleError("Artemis 没有返回有效的练习数据；接口可能已变化。")
    if not isinstance(data.get("participationResults", []), list):
        raise MoodleError("Artemis 练习结果列表格式不认识；接口可能已变化。")
    now = now or datetime.now(timezone.utc)
    results = {}
    for result in data.get("participationResults") or []:
        if not isinstance(result, dict):
            raise MoodleError("Artemis 练习结果格式不认识；接口可能已变化。")
        if isinstance(result, dict) and isinstance(result.get("score"), (int, float)):
            results.setdefault(result.get("participationId"), []).append(result)
    exercises = []
    for exercise in data.get("exercises") or []:
        if not isinstance(exercise, dict) or type(exercise.get("id")) is not int:
            continue
        _string_fields(exercise, ("title", "type", "releaseDate", "startDate", "dueDate", "assessmentDueDate"), "练习")
        if not isinstance(exercise.get("studentParticipations", []), list):
            raise MoodleError("Artemis 练习参与记录格式不认识；接口可能已变化。")
        if any(not isinstance(p, dict) for p in exercise.get("studentParticipations", [])):
            raise MoodleError("Artemis 练习参与记录格式不认识；接口可能已变化。")
        release, start, due = (_when(exercise.get(k)) for k in ("releaseDate", "startDate", "dueDate"))
        opens = start or release
        if opens and opens > now:
            status = "not_released"
        elif exercise.get("quizEnded") is True or (due and due <= now):
            status = "closed"
        else:
            status = "open"
        graded, practice, any_result = [], [], False
        participations = [p for p in exercise.get("studentParticipations") or [] if isinstance(p, dict)]
        for participation in participations:
            for result in results.get(participation.get("id"), []):
                any_result = True
                if result.get("rated") is not False or participation.get("testRun"):
                    (practice if participation.get("testRun") else graded).append(float(result["score"]))
        score, max_points = _best(graded), exercise.get("maxPoints")
        exercises.append({
            "id": exercise["id"], "course_id": course_id, "type": exercise.get("type"),
            "title": exercise.get("title") or "",
            "release_date": exercise.get("releaseDate"), "start_date": exercise.get("startDate"),
            "due_date": exercise.get("dueDate"), "assessment_due_date": exercise.get("assessmentDueDate"),
            "status": status,
            "counts": _COUNTS.get(exercise.get("includedInOverallScore"), exercise.get("includedInOverallScore")),
            "max_points": max_points, "bonus_points": exercise.get("bonusPoints"),
            "difficulty": exercise.get("difficulty"), "categories": _categories(exercise.get("categories")),
            "team": bool(exercise.get("teamMode")),
            "participation": "result" if any_result else "started" if participations else "not_started",
            "score": score, "practice_score": _best(practice),
            "points": (round(score * max_points / 100, 2)
                       if score is not None and isinstance(max_points, (int, float)) else None),
            "url": f"{ARTEMIS_URL}/courses/{course_id}/exercises/{exercise['id']}",
        })
    if data.get("exercises") and not exercises:
        raise MoodleError("Artemis 练习列表没有可识别的练习；接口可能已变化。")
    return sorted(exercises, key=lambda e: (e["due_date"] is None, _when(e["due_date"]) or now, e["id"]))


REPO_PREFIX = ARTEMIS_URL + "/git/"


def repo_url(value) -> str | None:
    """A participation's clone URL if it is a plain https://artemis.tum.de/git/<KEY>/<name>.git, else None."""
    if not isinstance(value, str) or not value.startswith(REPO_PREFIX) or not value.endswith(".git"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git", value[len(REPO_PREFIX):]):
        return None  # no user info, query, fragment or extra path segments
    return value


def pick_repositories(participations: list[dict]) -> dict:
    """Which clone URLs the exercise page shows, from /details' studentParticipations.

    Each participation is a dict as Artemis returns it, e.g. {"id", "testRun",
    "initializationState", "attempt", "repositoryUri", ...}; testRun=True is the
    practice-mode repository (…-practice-<login>.git), False the graded one.
    Returns {"practice": url or None, "graded": url or None}; pass every URL
    through repo_url() so only real Artemis git URLs reach the page.
    """
    def rank(participation):
        # Prefer a live repository; among several, the latest attempt, then the newest record.
        # INACTIVE ones are kept as a fallback: Artemis can resume them and the repository stays.
        attempt, ident = participation.get("attempt"), participation.get("id")
        return (participation.get("initializationState") in ("INITIALIZED", "FINISHED"),
                attempt if type(attempt) is int else -1, ident if type(ident) is int else -1)

    chosen = {}
    for participation in sorted(participations, key=rank, reverse=True):
        url = repo_url(participation.get("repositoryUri"))
        kind = "practice" if participation.get("testRun") is True else "graded"
        if url and kind not in chosen:
            chosen[kind] = url
    return {"practice": chosen.get("practice"), "graded": chosen.get("graded")}


def parse_exercise_repositories(data, exercise_id: int) -> dict:
    """{practice, graded} clone URLs from GET /api/exercise/exercises/<id>/details."""
    exercise = data.get("exercise") if isinstance(data, dict) else None
    if not isinstance(exercise, dict) or exercise.get("id") != exercise_id:
        raise MoodleError("Artemis 练习详情格式不认识；接口可能已变化。")
    participations = exercise.get("studentParticipations", [])
    if not isinstance(participations, list) or any(not isinstance(p, dict) for p in participations):
        raise MoodleError("Artemis 练习参与记录格式不认识；接口可能已变化。")
    return pick_repositories(participations)


def response_filename(response, url: str) -> str:
    """Content-Disposition's file name, else the URL's last segment."""
    message = Message()
    message["content-disposition"] = response.headers.get("Content-Disposition", "")
    name = message.get_filename()
    return name or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "attachment"


def _forum_rows(data, label):
    if not isinstance(data, list) or any(
            not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 for row in data):
        raise MoodleError(f"Artemis {label}格式不认识；接口可能已变化。")
    if len({row["id"] for row in data}) != len(data):
        raise MoodleError(f"Artemis {label}包含重复编号；不能确认读取范围。")
    return data


def parse_channels(data, course_id: int) -> list[dict]:
    """Only course channels, never direct or group chats; omit member/creator details."""
    channels = []
    for row in _forum_rows(data, "频道列表"):
        if row.get("type") != "channel":
            raise MoodleError("Artemis 返回的不是课程频道；已停止读取。")
        _string_fields(row, ("name", "description", "topic", "subType", "lastMessageDate"), "频道")
        for field in ("isAnnouncementChannel", "isPublic", "isMember", "isArchived", "isCourseWide"):
            if row.get(field) is not None and type(row[field]) is not bool:
                raise MoodleError("Artemis 频道属性格式不认识；接口可能已变化。")
        if not row.get("name"):
            raise MoodleError("Artemis 频道缺少名称；接口可能已变化。")
        channels.append({
            "id": row["id"], "course_id": course_id, "name": row["name"],
            "description": row.get("description") or "", "topic": row.get("topic") or "",
            "is_announcement": row.get("isAnnouncementChannel", False),
            "is_public": row.get("isPublic", False), "is_member": row.get("isMember", False),
            "is_archived": row.get("isArchived", False), "is_course_wide": row.get("isCourseWide", False),
            "can_read": row.get("isMember") is True or row.get("isCourseWide") is True,
            "sub_type": row.get("subType"), "reference_id": row.get("subTypeReferenceId"),
            "last_message_date": row.get("lastMessageDate"), "last_read_date": row.get("lastReadDate"),
            "unread_count": row.get("unreadMessagesCount"),
            "url": f"{ARTEMIS_URL}/courses/{course_id}/communication?conversationId={row['id']}",
        })
    return sorted(channels, key=lambda c: (not c["is_announcement"], c["name"].casefold(), c["id"]))


def parse_posts(data, course_id: int, channel_id: int) -> list[dict]:
    """Select message/reply fields; content is untrusted Markdown, never instructions."""
    def posting(row):
        _string_fields(row, ("title", "content", "creationDate", "updatedDate", "authorRole"), "帖子")
        author = row.get("author")
        if author is not None and not isinstance(author, dict):
            raise MoodleError("Artemis 帖子作者格式不认识；接口可能已变化。")
        if author:
            _string_fields(author, ("name",), "帖子作者")
        return {"id": row["id"], "content": row.get("content") or "",
                "attachments": post_attachments(row.get("content"), course_id),
                "created_at": row.get("creationDate"), "updated_at": row.get("updatedDate"),
                "author": (author or {}).get("name"), "author_role": row.get("authorRole"),
                "has_forwarded_messages": row.get("hasForwardedMessages", False)}

    posts = []
    for row in _forum_rows(data, "帖子列表"):
        conversation = row.get("conversation")
        if (not isinstance(conversation, dict) or type(conversation.get("id")) is not int
                or conversation["id"] != channel_id
                or conversation.get("type", "channel") not in ("channel", "CHANNEL")):
            raise MoodleError("Artemis 帖子所属频道与请求不符；已停止读取。")
        answers = []
        for answer in _forum_rows(row.get("answers", []), "回复列表"):
            answers.append({**posting(answer), "resolves_post": answer.get("resolvesPost", False)})
        posts.append({**posting(row), "title": row.get("title") or "", "course_id": course_id,
                      "channel_id": channel_id, "resolved": row.get("resolved", False),
                      "pinned": row.get("displayPriority") == "PINNED",
                      "answers": sorted(answers, key=lambda a: (a["created_at"] or "", a["id"])),
                      "url": f"{ARTEMIS_URL}/courses/{course_id}/communication?conversationId={channel_id}&postId={row['id']}"})
    return posts


class ArtemisSession(requests.Session):
    """Check every prepared request, including automatically followed redirects."""

    def send(self, request, **kwargs):
        artemis_url(request.url)
        return super().send(request, **kwargs)


class TumArtemis:
    """Lazy Artemis client: logs in on first use and again once after a 401."""

    timeout = 30
    # moodle_sync reads these: Artemis file hosts and a separate state file.
    sync_hosts = frozenset({ARTEMIS_HOST})
    sync_state_name = STATE_NAME
    sync_layout = "kind"

    def __init__(self, credentials):
        self._credentials = credentials  # () -> (username, password, totp_secret)
        self._courses_cache = None
        self._courses_cached_at = 0.0
        self._credentials_rejected = False
        self._channels_cache = {}
        self._repos_cache = {}  # exercise id -> (participation ids, repositories)
        self.session = ArtemisSession()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (compatible; tum-moodle-helper)"

    def _logged_in(self) -> bool:
        return any(c.name == "jwt" and ARTEMIS_HOST in c.domain for c in self.session.cookies)

    def login(self):
        """Send the stored password once; a refusal is reported, never retried."""
        if self._credentials_rejected:
            raise CredentialsRejected("Artemis 已拒绝已存凭据；请重新 setup 后重启本程序。")
        self.session.cookies.clear()
        self._courses_cache = None
        self._channels_cache.clear()
        username, password, _ = self._credentials()
        response = self.session.post(
            ARTEMIS_URL + LOGIN_PATH, allow_redirects=False, timeout=self.timeout,
            json={"username": username, "password": password, "rememberMe": True})
        with response:
            if response.status_code == 401:
                self._credentials_rejected = True
                raise CredentialsRejected("Artemis 拒绝了已存的 TUM ID 或密码；请确认能在网页登录，"
                                          "必要时运行 setup 重新保存后重启本程序。")
            if response.status_code == 429:
                raise MoodleError("Artemis 登录过于频繁，请稍后再试。")
            response.raise_for_status()
        if not self._logged_in():
            raise MoodleError("Artemis 登录没有返回登录 cookie；接口可能已变化。")

    def sync_request(self, method, url, **kwargs):
        """Protected read transfer with one authentication recovery and bounded retries."""
        url = artemis_url(url)
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            raise MoodleError("同步传输只允许 GET 和 HEAD。")
        if self._credentials_rejected:
            raise CredentialsRejected("Artemis 已拒绝已存凭据；请重新 setup 后重启本程序。")
        # Artemis never follows redirects, even if a generic sync caller asks to.
        kwargs.pop("allow_redirects", None)
        timeout = kwargs.pop("timeout", self.timeout)
        authenticated = False
        if not self._logged_in():
            self.login()
            authenticated = True
        retries = 0
        while True:
            response = None
            try:
                request = self.session.get if method == "GET" else self.session.head
                response = request(url, allow_redirects=False, timeout=timeout, **kwargs)
                if response.status_code in RETRY_STATUSES and retries < TRANSFER_RETRIES:
                    delay = transfer_retry_delay(response, retries)
                    response.close()
                    retries += 1
                    time.sleep(delay)
                    continue
                if response.status_code == 401:
                    raise NotLoggedIn("Artemis 重新登录后仍拒绝访问。")
                if response.is_redirect:
                    raise MoodleError("Artemis 返回了意外的跳转，已停止。")
                response.raise_for_status()
                if method == "GET" and kwargs.get("stream") and buffered_login_page(response):
                    raise NotLoggedIn("Artemis 返回登录页面，未保存为课程文件。")
                return response
            except NotLoggedIn:
                if response is not None:
                    response.close()
                if authenticated:
                    raise
                authenticated = True
                self.login()
            except (requests.Timeout, requests.ConnectionError):
                if response is not None:
                    response.close()
                if retries >= TRANSFER_RETRIES:
                    raise
                delay = transfer_retry_delay(None, retries)
                retries += 1
                time.sleep(delay)
            except Exception:
                if response is not None:
                    response.close()
                raise

    def _get(self, url, **kwargs):
        return self.sync_request("GET", url, **kwargs)

    def courses(self) -> list[dict]:
        courses = parse_courses(self._get("/api/course/courses/for-dashboard").json())
        self._courses_cache = courses
        self._courses_cached_at = time.monotonic()
        return courses

    def course(self, key) -> dict:
        if isinstance(key, dict):
            return key
        if (self._courses_cache is None
                or time.monotonic() - self._courses_cached_at >= COURSE_CACHE_TTL):
            self.courses()
        courses = self._courses_cache
        k = str(key).strip().lower()
        exact = [c for c in courses if str(c["id"]) == k or c["short_name"].lower() == k]
        if exact:
            return exact[0]
        matches = [c for c in courses if k in c["short_name"].lower() or k in c["title"].lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MoodleError(f"No Artemis course matches {key!r}")
        names = ", ".join(f"{c['short_name']} ({c['id']})" for c in matches)
        raise MoodleError(f"{key!r} matches several Artemis courses: {names}")

    def lectures(self, course) -> list[dict]:
        selected = self.course(course)
        data = self._get(f"/api/lecture/courses/{selected['id']}/lectures-with-slides").json()
        return parse_lectures(data, selected["id"])

    def exercises(self, course) -> list[dict]:
        selected = self.course(course)
        data = self._get(f"/api/course/courses/{selected['id']}/exercises-for-overview").json()
        return parse_exercises(data, selected["id"])

    def exercise_repositories(self, course) -> list[dict]:
        """exercises() plus each programming exercise's practice/graded clone URLs.

        The overview has no repository URLs, only participation ids, so /details is
        read once per programming exercise that has a participation, and again only
        when its participation ids change (e.g. practice mode was started on the web).
        Read-only: never starts a participation. One failed exercise does not stop the rest.
        """
        selected = self.course(course)
        data = self._get(f"/api/course/courses/{selected['id']}/exercises-for-overview").json()
        exercises = parse_exercises(data, selected["id"])
        raw = {e.get("id"): e for e in data.get("exercises") or [] if isinstance(e, dict)}
        for exercise in exercises:
            exercise["repositories"] = {"practice": None, "graded": None}
            if exercise["type"] != "programming":
                continue
            ids = tuple(sorted(str(p.get("id")) for p in raw[exercise["id"]].get("studentParticipations") or []))
            if not ids:
                continue
            cached = self._repos_cache.get(exercise["id"])
            if cached and cached[0] == ids:
                exercise["repositories"] = dict(cached[1])
                continue
            try:
                details = self._get(f"/api/exercise/exercises/{exercise['id']}/details").json()
                repositories = parse_exercise_repositories(details, exercise["id"])
            except (MoodleError, requests.RequestException, ValueError):
                exercise["repositories_error"] = "仓库地址暂时读不到，请稍后刷新。"
                continue
            self._repos_cache[exercise["id"]] = (ids, repositories)
            exercise["repositories"] = dict(repositories)
        return exercises

    def channels(self, course) -> list[dict]:
        selected = self.course(course)
        with self._get(f"/api/communication/courses/{selected['id']}/channels/overview") as response:
            channels = parse_channels(response.json(), selected["id"])
        self._channels_cache[selected["id"]] = (channels, time.monotonic())
        return channels

    def posts(self, course, channel: str, page: int = 0, page_size: int = 20, search: str = "") -> dict:
        """One bounded page, newest first, with embedded replies. Never marks as read."""
        if type(page) is not int or not 0 <= page <= 10000:
            raise MoodleError("page 必须是 0 到 10000 的整数。")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise MoodleError("page_size 必须是 1 到 100 的整数。")
        if not isinstance(search, str) or len(search) > 500:
            raise MoodleError("search 必须是最多 500 字符的文字。")
        if not isinstance(channel, str) or not channel.strip():
            raise MoodleError("请指定频道编号或完整频道名称。")
        selected = self.course(course)
        cached, timestamp = self._channels_cache.get(selected["id"], (None, 0))
        channels = cached if cached is not None and time.monotonic() - timestamp < COURSE_CACHE_TTL else self.channels(selected)
        key = channel.strip().casefold()
        matches = [c for c in channels if str(c["id"]) == key]
        if not matches:
            matches = [c for c in channels if c["name"].casefold() == key.lstrip("#")]
        if len(matches) != 1:
            raise MoodleError("频道不存在或名称不唯一；请先列出频道并使用编号。")
        chosen = matches[0]
        if not chosen["can_read"]:
            raise MoodleError("该频道尚未加入，不能只读获取帖子；如需要，请先在 Artemis 网页自行加入。")
        # 10.3/10.6 getMessages updates lastRead when searchText is NULL. An explicit
        # empty searchText disables that write, while MessageSpecs treats it as no
        # text filter. Do not omit this parameter, even for an unfiltered read.
        params = {"conversationIds": str(chosen["id"]), "searchText": search,
                  "postSortCriterion": "CREATION_DATE", "sortingOrder": "DESCENDING",
                  "pagingEnabled": "true", "page": page, "size": page_size}
        with self._get(f"/api/communication/courses/{selected['id']}/messages", params=params) as response:
            posts = parse_posts(response.json(), selected["id"], chosen["id"])
            count = response.headers.get("X-Total-Count")
        if len(posts) > page_size or (count is not None and not re.fullmatch(r"\d+", count)):
            raise MoodleError("Artemis 帖子分页格式不认识；不能确认读取范围。")
        total = int(count) if count is not None else None
        if posts and total is not None and total < page * page_size + len(posts):
            raise MoodleError("Artemis 帖子分页数量不一致；请重新读取。")
        has_more = (page + 1) * page_size < total if total is not None else len(posts) == page_size
        return {"course_id": selected["id"], "channel": chosen, "posts": posts,
                "page": page, "page_size": page_size, "total": total,
                "has_more": has_more, "next_page": page + 1 if has_more else None,
                "search": search, "order": "newest_first",
                "coverage": "单页主帖及接口附带的可见回复；转发原帖未展开；"
                            "附件只列出链接（attachments），可用 artemis_download 下载。"}

    # moodle_sync interface: one "resource" activity per attachment, its kind folder = section.
    def activities(self, course) -> list[dict]:
        return [{"id": f["sync_id"], "name": f["filename"], "type": "resource", "url": f["url"],
                 "section": f["kind"], "kind": f["kind"], "available": True,
                 "course_id": f["course_id"], "filename": f["filename"]}
                for f in material_layout(self.lectures(course))]

    def files(self, activity) -> list[dict]:
        url = activity.get("url")
        return [{"name": activity["filename"], "url": file_url(url)}] if url else []

    def sync_course(self, course, dest_dir, dry_run: bool = True) -> dict:
        if not self._logged_in():
            self.login()  # sync's HEAD/GET use the session directly.
        course_id = self.course(course)["id"]
        return moodle_sync.sync_course(self, course_id, dest_dir, dry_run=dry_run,
                                       state_name=f".artemis-sync-{course_id}.json")

    def download(self, url: str, dest_dir) -> list[Path]:
        """Save one lecture attachment or channel file under its server name; never overwrite."""
        url = file_url(url)
        directory = Path(dest_dir).expanduser()
        secrets = moodle_sync._secrets(self)
        values = {variant for secret in secrets for variant in
                  (secret, unquote(secret), unquote(unquote(secret))) if variant}
        if os.name != "nt":
            raise MoodleError("实际文件下载目前只支持 Windows。")
        with self._get(url, stream=True) as response:
            target = Utils.unique_path(directory / moodle_sync.safe_name(response_filename(response, url)))
            if any(secret in str(target) for secret in values | moodle_sync._secrets(self)):
                raise MoodleError("保存路径含会话信息，已拒绝写入。")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + "." + uuid4().hex[:8] + ".part")
            chunks = iter(moodle_sync.safe_download_chunks(response, self, initial_secrets=secrets))
            first = next(chunks, b"")
            with temporary.open("xb") as handle:
                if first:
                    handle.write(first)
                for chunk in chunks:
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        # Windows rename refuses an existing destination; a failed .part is kept.
        os.rename(temporary, target)
        return [target]
