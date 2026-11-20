"""Read the user's course map (<study root>\\课程对照表.md) into local sync targets.

Pure parsing: no network, no credentials. Each Markdown table row names a course
folder with its Moodle and Artemis ids; a "## …（`Semester …`）" heading gives the
semester folder. A row with an empty folder cell continues the folder above it.
Moodle and Artemis both sync into the course's Official folder by default; a
"… → `Official\\Somewhere\\`" suffix in the course cell overrides the Moodle target.
An optional 状态 column containing 已完成 (or "（已完成）" in the course cell) marks a
course the user has finished, e.g. a block course whose exam is over.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

MAP_NAME = "课程对照表.md"
# The study root is the folder that contains this project, unless TUM_STUDY_ROOT names another one.
STUDY_ROOT = Path(os.environ.get("TUM_STUDY_ROOT") or Path(__file__).resolve().parent.parent)
DEFAULT_MAP = STUDY_ROOT / MAP_NAME


@dataclass
class CourseEntry:
    semester: str
    folder: str
    title: str
    moodle_id: int | None
    artemis_id: int | None
    course_dir: str
    moodle_dest: str | None
    artemis_dest: str | None
    inbox: str
    completed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _first_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _relative(text: str) -> str:
    """A backticked relative folder such as `Official\\Moodle Lecture\\` without slashes at the ends."""
    return text.strip().strip("`").strip("\\/").strip()


def parse_course_map(text: str, root: str | Path) -> list[CourseEntry]:
    root = Path(root)
    entries = []
    semester = None
    columns = None
    folder = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            match = re.search(r"`([^`]*Semester[^`]*)`", stripped)
            semester = _relative(match.group(1)) if match else None
            columns = folder = None
            continue
        if not stripped.startswith("|"):
            columns = folder = None
            continue
        cells = _cells(stripped)
        if columns is None:
            columns = {}
            for index, name in enumerate(cells):
                if "文件夹" in name:
                    columns.setdefault("folder", index)
                elif "状态" in name:
                    columns["status"] = index
                elif "Moodle" in name and "编号" in name:
                    columns["moodle"] = index
                elif "Artemis" in name:
                    columns["artemis"] = index
                elif "课程" in name:
                    columns["title"] = index
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells if cell):
            continue
        if semester is None or "folder" not in columns or "moodle" not in columns:
            continue

        def cell(key):
            index = columns.get(key)
            return cells[index] if index is not None and index < len(cells) else ""

        if cell("folder"):
            folder = _relative(cell("folder"))
        moodle_id, artemis_id = _first_int(cell("moodle")), _first_int(cell("artemis"))
        if not folder or (moodle_id is None and artemis_id is None):
            continue
        title, _, target = cell("title").partition("→")
        completed = "已完成" in cell("status") or "（已完成）" in title
        title = title.replace("（已完成）", "")
        course_dir = root / semester / folder
        # Moodle and Artemis files share Official\ (sorted into Slides, Exercises, Exams, Other).
        moodle_target = course_dir / (_relative(target) if target.strip() else "Official")
        entries.append(CourseEntry(
            semester=semester, folder=folder, title=title.strip(),
            moodle_id=moodle_id, artemis_id=artemis_id, course_dir=str(course_dir),
            moodle_dest=str(moodle_target) if moodle_id is not None else None,
            artemis_dest=str(course_dir / "Official") if artemis_id is not None else None,
            inbox=str(course_dir / "_Inbox"), completed=completed))
    return entries


def load_course_map(path: str | Path = DEFAULT_MAP) -> list[CourseEntry]:
    """Read the map next to the course folders; FileNotFoundError when it is missing."""
    path = Path(path)
    return parse_course_map(path.read_text(encoding="utf-8"), path.parent)
