"""Read the user's Markdown planning settings; no generated second configuration."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path


class SettingsError(ValueError):
    pass


def _cells(line):
    return [part.strip().strip('`') for part in line.strip().strip('|').split('|')]


def load_settings(path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as exc:
        raise SettingsError('无法读取规划设定.md。') from exc
    courses, special, budgets = {}, {}, {}
    ratio_group = None
    section = ''
    course_base = None
    match = re.search(r'本地文件夹（在\s*`([^`]+)`', text)
    if match:
        course_base = match.group(1)
    for line in text.splitlines():
        if line.startswith('## '):
            section = line
        if not line.lstrip().startswith('|'):
            continue
        cells = _cells(line)
        if len(cells) >= 3 and cells[0] in ('周一至周五', '周六', '周日'):
            course_text = cells[1]
            number = re.search(r'按\s*(\d+(?:\.\d+)?)\s*小时', course_text)
            if not number:
                number = re.search(r'(\d+(?:\.\d+)?)\s*小时', course_text)
            research = re.search(r'(\d+(?:\.\d+)?)\s*小时', cells[2])
            if not number or not research:
                raise SettingsError('时间表需要明确的课程和科研与课外小时数。')
            limits = re.search(r'(\d+(?:\.\d+)?)\s*[–—-]\s*(\d+(?:\.\d+)?)\s*小时', course_text)
            hours = float(number.group(1))
            maximum = float(limits.group(2)) if limits else hours
            entry = {'course_minutes': round(hours * 60), 'course_max_minutes': round(maximum * 60),
                     'research_minutes': round(float(research.group(1)) * 60)}
            for weekday in (range(5) if cells[0] == '周一至周五' else [5 if cells[0] == '周六' else 6]):
                budgets[str(weekday)] = entry.copy()
        if len(cells) >= 4 and re.fullmatch(r'[A-Z]+\d+', cells[0]):
            key = cells[0]
            ratio = re.search(r'(\d+(?:\.\d+)?)\s*%', cells[3])
            if ratio:
                ratio_group = key
            elif '同上' not in cells[3]:
                raise SettingsError(f'{key} 缺少课程比例。')
            entry = {'name': cells[1], 'english_name': cells[2],
                     'ratio_group': ratio_group, 'ratio': float(ratio.group(1)) if ratio else None}
            if len(cells) >= 8 and course_base:
                # Native Windows paths are intentionally retained on Windows.
                entry['folder'] = str(Path(course_base) / cells[7])
            courses[key] = entry
        if '特殊日期' in section and cells and re.fullmatch(r'\d{4}-\d{2}-\d{2}', cells[0]):
            try:
                date.fromisoformat(cells[0])
            except ValueError as exc:
                raise SettingsError('特殊日期中有不存在的日期。') from exc
            rule = cells[2] if len(cells) > 2 else ''
            if re.search(r'不排任务|不安排任务|休息', rule):
                special[cells[0]] = {'course_minutes': 0, 'course_max_minutes': 0, 'research_minutes': 0,
                                     'note': cells[1]}
            else:
                course = re.search(r'课程\s*(\d+(?:\.\d+)?)\s*小时', rule)
                research = re.search(r'(?:科研(?:与课外)?|课外)\s*(\d+(?:\.\d+)?)\s*小时', rule)
                if not course:
                    raise SettingsError(f'{cells[0]} 的特殊日期规则无法确定课程时长，请写明“课程 X 小时”。')
                minutes = round(float(course.group(1)) * 60)
                special[cells[0]] = {'course_minutes': minutes, 'course_max_minutes': minutes,
                                     'research_minutes': round(float(research.group(1)) * 60) if research else 0,
                                     'note': cells[1]}
    if len(budgets) != 7 or not courses:
        raise SettingsError('规划设定需要完整的每周时间表和课程表。')
    return {'path': str(path), 'budgets': budgets, 'courses': courses, 'special_dates': special,
            'research_week_minutes': sum(entry['research_minutes'] for entry in budgets.values())}


def budget_for(settings: dict, day: str) -> dict:
    return dict(settings['special_dates'].get(day) or settings['budgets'][str(date.fromisoformat(day).weekday())])
