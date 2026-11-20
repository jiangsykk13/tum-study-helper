"""Pure, offline rendering helpers for Moodle HTML and course-format state.

HTML is treated only as text. No page content is executed and these helpers do
not make requests, read credentials, or write files.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


_BLOCKS = {
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "header",
    "main", "p", "pre", "section", "summary",
}
_TABLE_COLUMNS = 256


def _plain(value: object) -> str:
    return html.unescape(str(value or "")).strip()


def _number(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _ordered_modules(state: dict) -> list[tuple[dict, dict]]:
    """Return module/section pairs in section-number and cmlist order.

    A module missing from cmlist is placed at the end of its section, preserving
    its order in ``cm``. This also permits useful output from incomplete state.
    """
    if not isinstance(state, dict):
        return []
    raw_cms = state.get("cm")
    raw_sections = state.get("section")
    cms = [cm for cm in raw_cms if isinstance(cm, dict)] if isinstance(raw_cms, list) else []
    sections = [sec for sec in raw_sections if isinstance(sec, dict)] if isinstance(raw_sections, list) else []
    sections = sorted(sections, key=lambda sec: _number(sec.get("number")))
    by_id = {str(cm.get("id")): cm for cm in cms}
    seen: set[str] = set()
    result = []
    for sec in sections:
        cmlist = sec.get("cmlist")
        for cmid in cmlist if isinstance(cmlist, list) else []:
            key = str(cmid)
            cm = by_id.get(key)
            if cm is not None and key not in seen:
                result.append((cm, sec))
                seen.add(key)
        for cm in cms:
            key = str(cm.get("id"))
            if key not in seen and _number(cm.get("sectionnumber"), -1) == _number(sec.get("number")):
                result.append((cm, sec))
                seen.add(key)
    for cm in cms:
        key = str(cm.get("id"))
        if key not in seen:
            result.append((cm, {"number": _number(cm.get("sectionnumber")), "title": ""}))
            seen.add(key)
    return result


def activity_order(state: dict) -> dict[int, int]:
    """Map cmid to its course position for sorting ordinary activities + labels.

    ``sorted(activities, key=lambda a: activity_order(state).get(a.id, ...))``
    keeps labels between the original neighbouring activities. Compute the map
    once. It is internal sorting information and need not be exposed by MCP.
    """
    return {_number(cm.get("id")): position
            for position, (cm, _sec) in enumerate(_ordered_modules(state))
            if _number(cm.get("id")) > 0}


def label_activities(state: dict, course_id: int) -> list[dict]:
    """Extract text-only labels with exactly the fields of moodlekit.Activity.

    Moodle's state contains the label's short name, not its full HTML content.
    Full text can be read separately from the course page. No label URL is
    invented; unavailable labels remain visible with ``available=False``.
    """
    result = []
    for cm, sec in _ordered_modules(state):
        if cm.get("module") != "label" or cm.get("url") or _number(cm.get("id")) <= 0:
            continue
        visible = cm.get("uservisible", True)
        available = visible not in (False, 0, "0", "false", "False", None)
        result.append({
            "name": _plain(cm.get("name")), "type": "label", "url": None,
            "section": _plain(sec.get("title")),
            "section_number": _number(sec.get("number", cm.get("sectionnumber"))),
            "available": available, "course_id": int(course_id),
            "id": _number(cm.get("id")),
        })
    return result


def _text_node(node: NavigableString) -> str:
    if isinstance(node, Comment):
        return ""
    return re.sub(r"\s+", " ", str(node))


def _render_children(node: Tag, depth: int = 0) -> str:
    return "".join(_render(child, depth) for child in node.children)


def _render_list(node: Tag, depth: int) -> str:
    lines = []
    for item in node.find_all("li", recursive=False):
        item_lines = [line.rstrip() for line in _render_children(item, depth + 1).splitlines() if line.strip()]
        indent = "  " * depth
        if item_lines:
            # Nested lists already have their own indentation and prefixes.
            # Keeping the rendered lines in place preserves text after a list.
            first = item_lines[0]
            nested_indent = "  " * (depth + 1)
            if first.startswith(nested_indent + "- "):
                lines.extend([indent + "-", first])
            else:
                lines.append(indent + "- " + first.strip())
            for line in item_lines[1:]:
                lines.append(line if line.startswith(nested_indent) else indent + "  " + line.strip())
    return "\n" + "\n".join(lines) + "\n"


def _cell_text(cell: Tag) -> str:
    text = _render_children(cell)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    # A Markdown table row must stay on one physical line. Backslashes are
    # escaped first, so literal backslashes next to a pipe cannot unescape it.
    return "<br>".join(line for line in lines if line).replace("\\", "\\\\").replace("|", "\\|")


def _render_table(table: Tag) -> str:
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if not rows:
        return ""
    grid: dict[tuple[int, int], str] = {}
    header_count = 0
    header_finished = False
    for r, row in enumerate(rows):
        cells = row.find_all(["th", "td"], recursive=False)
        in_head = row.find_parent("thead") is not None
        all_headers = bool(cells) and all(cell.name == "th" for cell in cells)
        if not header_finished and (in_head or all_headers):
            header_count += 1
        else:
            header_finished = True
        col = 0
        for cell in cells:
            while (r, col) in grid and col < _TABLE_COLUMNS:
                col += 1
            if col >= _TABLE_COLUMNS:
                break
            width = min(max(1, _number(cell.get("colspan"), 1)), _TABLE_COLUMNS - col)
            raw_height = _number(cell.get("rowspan"), 1)
            height = len(rows) - r if raw_height == 0 else min(max(1, raw_height), len(rows) - r)
            text = _cell_text(cell)
            for rr in range(r, r + height):
                for cc in range(col, col + width):
                    # Keep previously placed cells in malformed overlapping spans.
                    grid.setdefault((rr, cc), text)
            col += width
    width = max((col + 1 for _row, col in grid), default=0)
    if not width:
        return ""
    matrix = [[grid.get((r, col), "") for col in range(width)] for r in range(len(rows))]
    if header_count:
        headers = []
        for col in range(width):
            parts = []
            for r in range(header_count):
                value = matrix[r][col]
                if value and (not parts or value != parts[-1]):
                    parts.append(value)
            headers.append(" / ".join(parts))
    else:
        headers = [""] * width
    rendered = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * width) + " |"]
    rendered.extend("| " + " | ".join(row) + " |" for row in matrix[header_count:])
    caption = table.find("caption", recursive=False)
    if caption:
        rendered.insert(0, _render_children(caption).strip())
    return "\n\n" + "\n".join(rendered) + "\n\n"


def _render(node: object, depth: int = 0) -> str:
    if isinstance(node, NavigableString):
        return _text_node(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "table":
        return _render_table(node)
    if node.name in ("ul", "ol"):
        return _render_list(node, depth)
    if node.name == "br":
        return "\n"
    if node.name == "pre":
        return "\n" + node.get_text().strip("\r\n") + "\n"
    if node.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        text = re.sub(r"\s+", " ", _render_children(node, depth)).strip()
        return "\n\n" + "#" * int(node.name[1]) + " " + text + "\n\n"
    text = _render_children(node, depth)
    return "\n" + text + "\n" if node.name in _BLOCKS else text


def html_to_text(html: str) -> str:
    """Render the Moodle main region as readable text with Markdown structure.

    Tables repeat merged-cell text across the covered rows/columns and combine
    stacked headings. A table without a heading gets an empty Markdown header,
    preserving all of its data rows. Extremely wide tables are capped at 256
    columns. HTML without ``#region-main`` falls back to body/the document.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#region-main") or soup.body or soup
    for node in list(main.select("script, style, nav, .visually-hidden, .accesshide, .sr-only")):
        if node.parent is not None:
            node.decompose()
    text = _render_children(main)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
