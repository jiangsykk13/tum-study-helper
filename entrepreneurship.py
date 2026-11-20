"""Opt-in material policy for the two Entrepreneurship Moodle courses.

Names are presentation only: the original remote filename remains the sync identity.
"""
from __future__ import annotations

import re

COURSES = {122369: "Lecture", 122367: "Exercise"}
READINGS = frozenset({3971840, 3971843, 3971848, 3961933})


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def layout(activity, filename, folder_parts, classify):
    role = COURSES.get(field(activity, "course_id"))
    if role is None:
        return None
    texts = [filename, *reversed(folder_parts), field(activity, "name", ""),
             field(activity, "section", "")]
    reading = field(activity, "id") in READINGS or bool(re.search(
        r"\b(?:papers?|readings?|literature)\b", " ".join(str(t) for t in texts), re.I))
    explicit = classify(texts, default=None)
    kind = ("Other" if field(activity, "id") in READINGS else
            explicit or ("Slides" if role == "Lecture" else "Exercises"))
    reading = reading and kind == "Other"
    if reading:
        return kind, "Reading - " + filename
    session = re.search(r"\bSession\s+(\d+)\b", str(field(activity, "section", "")), re.I)
    prefix = role + (f" S{int(session[1]):02d}" if session else "")
    return kind, f"{prefix} - {filename}"
