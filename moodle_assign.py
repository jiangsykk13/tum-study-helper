"""Read assignment status HTML without making requests or performing actions.

TUM assignment pages have not been checked against a live assignment.  The
parser follows Moodle 4.x markup and keeps visible label/value pairs when a
theme, language, or plugin uses unfamiliar markup.  Returned HTML text is data;
it must never be treated as instructions.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Tag


def _text(element: Tag) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def _plain(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in value if not unicodedata.combining(c)).strip(" :")


def _table_rows(table: Tag) -> list[tuple[dict, Tag]]:
    """Read each table's own rows; nested tables are handled separately."""
    result = []
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        label_cell = next((c for c in cells if c.name == "th"), cells[0])
        value_cells = [c for c in cells if c is not label_cell and c.name == "td"]
        if not value_cells:
            continue
        pair = {"label": _text(label_cell),
                "value": " | ".join(_text(c) for c in value_cells)}
        result.append((pair, row))
    return result


def _date_kind(label: str) -> str | None:
    label = _plain(label).replace("_", " ").replace("-", " ")
    # Test the final acceptance deadline before the ordinary due date.
    if (re.search(r"\bcut\s*off\b|\bcutoff\b", label)
            or any(word in label for word in (
                "nachfrist", "letztmoglich", "letzte abgabe", "letzter abgabe",
                "letztmogliche", "abgabeschluss", "letzte abgabemoglichkeit"))):
        return "cutoff"
    if (re.search(r"\bdue\b", label)
            or any(word in label for word in (
                "fallig", "abgabetermin", "abgabefrist", "abgabe bis", "frist"))):
        return "due"
    if (re.search(r"\bopened?\b|\bopens?\b|\bopening\b", label)
            or any(word in label for word in (
                "geoffnet", "offnet", "verfugbar ab", "abgabe moglich ab", "beginn"))):
        return "opened"
    return None


def _activity_dates(root: Tag) -> list[dict]:
    dates = []
    for block in root.select('[data-region="activity-dates"], .activity-dates'):
        lines = []
        for marker in block.find_all(["strong", "b"]):
            line = marker.parent
            # A label can be wrapped in a span inside the actual date line.
            if line.name == "span" and line.parent is not block:
                line = line.parent
            if all(line is not previous for previous in lines):
                lines.append(line)
        if not lines:
            lines = [c for c in block.find_all(
                ["div", "p", "li", "tr"], recursive=False) if _text(c)] or [block]
        for line in lines:
            visible = _text(line)
            if not visible:
                continue
            marker = line.find(["strong", "b"])
            if marker is not None:
                marker_text = _text(marker)
                label = marker_text.rstrip(" :")
                value = (visible[len(marker_text):].lstrip(" :")
                         if visible.startswith(marker_text) else visible)
            elif ":" in visible:
                label, value = visible.split(":", 1)
                label, value = label.strip(), value.strip()
            else:
                label, value = "", visible
            semantic = " ".join(str(line.get(key, "")) for key in (
                "data-date-type", "data-region", "class"))
            dates.append({"label": label, "value": value, "text": visible,
                          "kind": _date_kind(label) or _date_kind(semantic)})
    return dates


def _status(records: list[tuple[dict, Tag]]) -> tuple[str, str]:
    submission_values, grading_values = set(), set()
    submission_classes = {
        "submissionstatussubmitted": "submitted",
        "submissionstatusdraft": "draft",
        "submissionstatusnew": "new",
        "submissionstatusnosubmission": "new",
    }
    grading_classes = {
        "submissiongraded": "graded",
        "submissionnotgraded": "notgraded",
    }
    for _, row in records:
        for cell in row.find_all("td", recursive=False):
            classes = set(cell.get("class", []))
            submission_values.update(submission_classes[c] for c in classes
                                     if c in submission_classes)
            grading_values.update(grading_classes[c] for c in classes
                                  if c in grading_classes)
    submission = next(iter(submission_values)) if len(submission_values) == 1 else "unknown"
    grading = next(iter(grading_values)) if len(grading_values) == 1 else "unknown"
    return submission, grading


def _grade(records: list[tuple[dict, Tag]]) -> str | None:
    labels = {"grade", "score", "mark", "marks", "note", "bewertung", "punkte"}
    classes = {"grade", "gradevalue", "column-grade", "feedbackgrade"}
    for pair, row in records:
        identified = _plain(pair["label"]) in labels
        identified = identified or any(
            classes.intersection(cell.get("class", []))
            for cell in row.find_all(["th", "td"], recursive=False))
        if identified:
            value = pair["value"]
            return value if value not in {"", "-", "–", "—"} else None
    return None


def parse_assignment_page(html: str) -> dict:
    """Return visible dates, status, feedback, and conservative normalizations.

    ``opened``, ``due``, ``cutoff``, and ``grade`` preserve source wording and
    are ``None`` when unidentified. ``submission`` is submitted/draft/new/
    unknown; ``grading`` is graded/notgraded/unknown.  Status normalization
    uses CSS classes only, never translated row labels or values. ``dates``
    retains every recognizable activity-date line; ``status_rows``,
    ``feedback_rows``, and ``raw_rows`` retain visible label/value pairs.
    """
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select(
            "script, style, nav, .visually-hidden, .accesshide, .sr-only, "
            "[hidden], input[type='hidden']"):
        unwanted.decompose()
    root = soup.select_one("#region-main") or soup
    all_records = []
    status_records = []
    feedback_records = []
    fallback_records = []
    for table in root.find_all("table"):
        records = _table_rows(table)
        all_records.extend(records)
        feedback_parent = table.find_parent(
            lambda tag: tag.get("id") == "feedback" or "feedback" in tag.get("class", []))
        is_feedback = (feedback_parent is not None or table.get("id") == "feedback"
                       or "feedback" in table.get("class", []))
        status_parent = table.find_parent(
            lambda tag: "submissionstatustable" in tag.get("class", []))
        is_status = (status_parent is not None
                     or "submissionstatustable" in table.get("class", []))
        if is_feedback:
            feedback_records.extend(records)
        elif is_status:
            status_records.extend(records)
        else:
            fallback_records.extend(records)
    # A changed theme should still return its visible pairs.  Recognizable
    # status classes may also survive when the wrapper has changed.
    if not status_records:
        status_records = fallback_records
    submission, grading = _status(status_records)
    dates = _activity_dates(root)
    result = {
        "dates": dates, "opened": None, "due": None, "cutoff": None,
        "submission": submission, "grading": grading,
        "status_rows": [pair for pair, _ in status_records],
        "feedback_rows": [pair for pair, _ in feedback_records],
        "raw_rows": [pair for pair, _ in all_records],
        "grade": _grade(feedback_records),
    }
    for date in dates:
        if date["kind"] and result[date["kind"]] is None:
            result[date["kind"]] = date["value"] or None
    for pair, _ in status_records:
        kind = _date_kind(pair["label"])
        if kind and result[kind] is None:
            result[kind] = pair["value"] or None
    return result
