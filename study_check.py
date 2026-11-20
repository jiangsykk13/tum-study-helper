"""Read-only site checks + existing course sync, with a durable local alert outbox.

TUMonline observations are supplied by browser use; this module never logs in to
TUMonline, exports browser cookies, or expands either client's host allowlist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import planner
from local_lock import directory_lock

STATE = '检查记录.json'
HOSTS = {'www.moodle.tum.de', 'artemis.tum.de', 'campus.tum.de'}
SOURCE = re.compile(r'(moodle|artemis|tumonline|gmail):[\w-]+')
# Only the thread part of a Gmail link is kept; `authuser=` would store the account address.
GMAIL_FRAGMENT = re.compile(r'(?:all|inbox)/(thread-f:\d+)(?:\|msg-f:\d+)?|(?:all|inbox)/([0-9a-f]{16})')
PRIVATE = re.compile(r'cookie|sesskey|password|secret|token|jwt|saml|authorization|session_code|client_data', re.I)


def stamp(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('检查时间必须含时区。')
    return value.astimezone(timezone.utc).isoformat(timespec='seconds')


def url(value, source=''):
    p = urlsplit(value)
    if source.startswith('gmail:') and p.hostname == 'mail.google.com':
        match = GMAIL_FRAGMENT.fullmatch(p.fragment)
        if (p.scheme != 'https' or p.port not in (None, 443) or p.username or p.password
                or not re.fullmatch(r'/mail/(?:u/\d/)?', p.path) or not match):
            raise ValueError('检查记录链接不符合站点限制。')
        return f'https://mail.google.com/mail/#all/{match.group(1) or match.group(2)}'
    if (p.scheme != 'https' or p.hostname not in HOSTS or p.port not in (None, 443)
            or p.username or p.password or PRIVATE.search(unquote(unquote(p.path)))
            or any(PRIVATE.search(unquote(unquote(k))) for k, _ in parse_qsl(p.query))
            or PRIVATE.search(unquote(unquote(p.fragment)))):
        raise ValueError('检查记录链接不符合站点限制。')
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, p.fragment))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def normalize(item, source):
    if not isinstance(item, dict) or not SOURCE.fullmatch(source):
        raise ValueError('检查来源或事项格式无效。')
    result = {k: item.get(k) for k in ('title', 'course_key', 'kind', 'url', 'due_at',
                                      'completed', 'grade', 'details', 'raw_due', 'merge_key')}
    if (not isinstance(result['title'], str) or not result['title'].strip()
            or result['kind'] not in ('assignment', 'quiz', 'project', 'exam', 'registration',
                                      'withdrawal', 'schedule', 'grade', 'notice')
            or not isinstance(result['course_key'], str)):
        raise ValueError('检查事项需要课程、标题和类型。')
    if result['completed'] is not None and type(result['completed']) is not bool:
        raise ValueError('完成状态必须明确或未知。')
    result['url'] = url(result['url'], source)
    result['due_at'] = stamp(result['due_at']) if result['due_at'] else None
    for key in ('details', 'raw_due', 'merge_key', 'grade'):
        if result[key] is not None and not isinstance(result[key], str):
            raise ValueError('检查说明和成绩须为原始文字。')
    if source.startswith('tumonline:') and not result.get('merge_key'):
        raise ValueError('TUMonline 列表中的事项须提供稳定 merge_key，以免同页不同考试或成绩合并。')
    planner.assert_safe_data(result)
    if len(json.dumps(result, ensure_ascii=False)) > 20000:
        raise ValueError('单条检查记录过长。')
    result['source'] = source
    # Same Moodle activity from the calendar and assignment page is one item.
    result['key'] = digest([result['course_key'], result['merge_key'] or result['url'], result['kind']])
    return result


def read(root):
    path = Path(root) / STATE
    if not path.exists():
        return {'version': 1, 'sources': {}, 'outbox': {}, 'sent': {}, 'thresholds': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or any(not isinstance(data.get(k), dict)
                for k in ('sources', 'outbox', 'sent', 'thresholds')):
            raise ValueError()
        aliases = {}
        for name, source in data['sources'].items():
            if not isinstance(source, dict) or not isinstance(source.get('items'), list):
                raise ValueError()
            normalized = []
            for item in source['items']:
                current = normalize(item, name)
                aliases[item.get('key')] = current['key']
                previous_signature = digest([item.get('key'), current.get('due_at')])
                signature = digest([current['key'], current.get('due_at')])
                if previous_signature in data['thresholds']:
                    data['thresholds'][signature] = sorted(set(data['thresholds'].get(signature, []) +
                                                               data['thresholds'][previous_signature]))
                normalized.append(current)
            source['items'] = normalized
        for event in data['outbox'].values():
            prior = event.get('item')
            if isinstance(prior, dict) and prior.get('key') in aliases:
                prior['key'] = aliases[prior['key']]
        planner.assert_safe_data(data)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, planner.PlanError):
        raise ValueError('检查记录损坏或含敏感信息，已停止覆盖。') from None
    return data


def _observation_rank(item):
    """Sort key for the observations of one item; the first one supplies the shown
    title, url and date (all dates are still compared for `conflict`)."""
    official = item['kind'] in ('exam', 'registration', 'withdrawal', 'grade', 'schedule')
    tumonline = item['source'].startswith('tumonline:')
    mail = item['source'].startswith('gmail:')
    # TODO(human): decide where a `gmail:` observation ranks against the platform ones.
    # Exact dates beat unknown dates; the platform owns its own activities.
    return (not bool(item.get('due_at')), not tumonline if official else tumonline, item['source'])


def grouped(data):
    groups = {}
    for source in data['sources'].values():
        for item in source.get('items', []):
            groups.setdefault(item['key'], []).append(item)
    result = {}
    for key, observations in groups.items():
        observations = sorted(observations, key=_observation_rank)
        chosen = dict(observations[0])
        chosen['observations'] = observations
        chosen['completed'] = (True if any(i['completed'] is True for i in observations)
                               else False if any(i['completed'] is False for i in observations) else None)
        dates = sorted({i['due_at'] for i in observations if i.get('due_at')})
        chosen['conflict'] = dates if len(dates) > 1 else []
        result[key] = chosen
    return result


def update(root, reports, *, now=None):
    """Apply scoped snapshots. Failed sources retain their last successful items."""
    checked = stamp(now)
    with directory_lock(Path(root) / STATE):
        data = read(root)
        before = grouped(data)
        for report in reports:
            name = report.get('source')
            if not isinstance(name, str) or not SOURCE.fullmatch(name):
                raise ValueError('检查来源无效。')
            if type(report.get('ok')) is not bool:
                raise ValueError('来源状态无效。')
            previous = data['sources'].get(name, {'items': [], 'last_success': None})
            current = dict(previous, checked_at=checked, ok=report['ok'])
            current['error'] = None if report['ok'] else report.get('error', '读取未完成。')
            if report['ok']:
                if not isinstance(report.get('items'), list):
                    raise ValueError('成功来源必须提供事项列表。')
                items = [normalize(i, name) for i in report['items']]
                if name.startswith('gmail:'):
                    items = _keep_mail_items(previous['items'], items, checked)
                current.update(items=items, last_success=checked)
            planner.assert_safe_data(current)
            data['sources'][name] = current
            if not current['ok']:
                event = {'type': 'source_failed', 'source': name, 'message': current['error'],
                         'last_success': current['last_success']}
                _enqueue(data, ['failure', name, current['error'], current['last_success']], event, checked)
            elif previous.get('ok') is False:
                _enqueue(data, ['recovered', name, previous.get('checked_at')],
                         {'type': 'source_recovered', 'source': name}, checked)
        for key, item in grouped(data).items():
            old = before.get(key)
            change = None
            if old is None:
                if item['kind'] != 'grade' or item.get('grade'):
                    change = 'new_grade' if item['kind'] == 'grade' else 'new'
            elif item['kind'] == 'grade' and item.get('grade') and item['grade'] != old.get('grade'):
                change = 'grade_changed'
            elif item.get('due_at') != old.get('due_at') or item.get('conflict') != old.get('conflict'):
                change = 'deadline_changed'
            elif item['kind'] in ('exam', 'schedule', 'registration', 'withdrawal', 'notice') and any(
                    item.get(k) != old.get(k) for k in ('title', 'details')):
                change = 'changed'
            due = item.get('due_at')
            hours = ((datetime.fromisoformat(due) - datetime.fromisoformat(checked)).total_seconds() / 3600
                     if due else None)
            signature = digest([key, due])
            crossed = [n for n in (168, 48, 24) if hours is not None and 0 <= hours <= n
                       and n not in data['thresholds'].get(signature, [])]
            # A first observation within 24 h emits a single urgent alert, not three.
            if crossed and item['completed'] is not True and item['kind'] not in ('grade', 'notice', 'schedule'):
                data['thresholds'][signature] = sorted(set(data['thresholds'].get(signature, []) + crossed))
                if not change:
                    change = f'due_{min(crossed)}h'
            if change == 'new' and hours is not None and hours < 0:
                change = None  # Establish an historical baseline without reviving expired work.
            if change and (item['completed'] is not True or change in ('new_grade', 'grade_changed')):
                event = {'type': change, 'item': item}
                _enqueue(data, [key, change, due, item.get('grade'), item.get('details'), item.get('conflict')],
                         event, checked)
        latest = grouped(data)
        for event_id, event in list(data['outbox'].items()):
            prior_item = event.get('item')
            current = latest.get(prior_item.get('key')) if prior_item else None
            if current and (current['completed'] is True or
                    (event['type'] in ('new', 'deadline_changed') or event['type'].startswith('due_')) and
                    (prior_item.get('due_at') != current.get('due_at') or
                     current.get('due_at') and current['due_at'] < checked)):
                if current['kind'] != 'grade':
                    data['outbox'].pop(event_id)
            elif event['type'] == 'source_failed' and data['sources'].get(event['source'], {}).get('ok') is True:
                data['outbox'].pop(event_id)
        data['checked_at'] = checked
        planner._write_json(Path(root) / STATE, data)
    return view(root)


def _keep_mail_items(previous, items, checked):
    """Mail is searched over a short window, so a snapshot only adds or corrects items.

    Earlier mail items stay until their own date has passed; a resubmitted item with
    the same key replaces the old one. Undated items are not carried: their `new`
    alert has already been queued and they never cross a deadline threshold.
    """
    merged = {i['key']: i for i in previous if i.get('due_at') and i['due_at'] >= checked}
    merged.update((i['key'], i) for i in items)
    return list(merged.values())


def _enqueue(data, identity, event, checked):
    key = digest(identity)
    if key not in data['sent']:
        data['outbox'].setdefault(key, dict(event, id=key, created_at=checked))


def view(root):
    data = read(root)
    return {'checked_at': data.get('checked_at'), 'sources': [dict(source=name,
            **{k: s.get(k) for k in ('ok', 'checked_at', 'last_success', 'error')})
            for name, s in data['sources'].items()],
            'items': sorted(grouped(data).values(), key=lambda i: (i.get('due_at') or '9999', i['title'])),
            'alerts': list(data['outbox'].values())}


def acknowledge(root, ids, *, now=None):
    with directory_lock(Path(root) / STATE):
        data = read(root)
        if any(key not in data['outbox'] and key not in data['sent'] for key in ids):
            raise ValueError('提醒编号不存在，未更改记录。')
        for key in ids:
            if key in data['outbox']:
                data['outbox'].pop(key)
                data['sent'][key] = stamp(now)
        planner._write_json(Path(root) / STATE, data)
    return {'ok': True, 'acknowledged': len(ids)}


def _sanitize(data, owner):
    from tum_moodle_auto import redact_secrets
    data = redact_secrets(data, owner._secrets())
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if PRIVATE.fullmatch(key):
                continue
            if key in ('url', 'href') and isinstance(value, str):
                try:
                    value = url(value)
                except ValueError:
                    continue
            result[key] = _sanitize(value, owner)
        return result
    if isinstance(data, list):
        return [_sanitize(value, owner) for value in data]
    if isinstance(data, str):
        return planner.redact_sensitive(data)
    return data


def _collect_artemis_forum(call, run, course_id, course_key):
    """Bounded channel snapshots for agent review; never infer deadlines from text."""
    prefix = f'artemis-{course_id}'
    paths, failures, truncated, skipped = [], [], [], []
    snapshot = {'source': 'artemis', 'course_key': course_key, 'course_id': course_id,
                'paths': paths, 'failed_channels': failures, 'truncated_channels': truncated,
                'skipped_channels': skipped,
                'coverage': '课程频道列表；每个可读频道最新最多60个主帖及附带回复；不含私信、转发原帖或附件。'}
    try:
        channels = call('artemis_channels', course=str(course_id))
        path = run / f'{prefix}-channels.json'
        planner._write_json(path, channels)
        paths.append(str(path))
    except Exception:
        snapshot['error'] = 'Artemis 频道列表读取失败。'
        return snapshot, {'source': f'artemis:forum-pages-{course_id}', 'ok': False,
                          'error': 'Artemis 频道列表读取失败；不能视为没有公告。'}
    snapshot['channel_count'] = len(channels)
    for channel in channels:
        channel_id = channel['id']
        if not channel['can_read']:
            skipped.append(channel_id)
            continue
        seen = set()
        try:
            for page in range(3):
                result = call('artemis_posts', course=str(course_id), channel=str(channel_id), page=page, page_size=20)
                ids = {post['id'] for post in result['posts']}
                if result['has_more'] and not (ids - seen):
                    raise ValueError('分页未前进。')
                seen.update(ids)
                path = run / f'{prefix}-channel-{channel_id}-posts-{page}.json'
                planner._write_json(path, result)
                paths.append(str(path))
                if not result['has_more']:
                    break
            else:
                truncated.append({'channel_id': channel_id, 'next_page': result['next_page']})
        except Exception:
            failures.append(channel_id)
    snapshot['ok'] = not failures
    return snapshot, {'source': f'artemis:forum-pages-{course_id}', 'ok': not failures, 'items': [],
                      'error': '部分 Artemis 频道读取失败；保留上次成功记录，不能视为没有公告。'}


def collect(root, map_path=None, *, sync=False, owner=None):
    """Collect through exactly the guarded functions used by the UI and MCP."""
    from course_map import load_course_map, DEFAULT_MAP
    from tum_moodle_auto import TumMcpServer
    from study_materials import ingest
    owner = owner or TumMcpServer()
    tools = owner.tool_functions()
    entries = [e for e in load_course_map(map_path or DEFAULT_MAP) if not e.completed]
    run = Path(root) / '检查' / planner.berlin_now().strftime('%Y-%m-%d_%H%M%S_%f')
    reports, receipts, pages = [], [], []
    names = tools['list_courses'](which='all')
    name_to_entry = {}
    if isinstance(names, list):
        for course in names:
            for entry in entries:
                if str(course.get('id')) == str(entry.moodle_id):
                    name_to_entry[course.get('name')] = entry

    def call(name, **kwargs):
        value = tools[name](**kwargs)
        if isinstance(value, dict) and 'error' in value:
            raise ValueError('来源读取失败；请检查网络、登录或页面结构。')
        return _sanitize(value, owner)

    try:
        deadlines = call('upcoming_deadlines', days=21)
        planner._write_json(run / 'deadlines.json', deadlines)
        items = []
        for d in deadlines:
            entry = name_to_entry.get(d.get('course'))
            if entry and d.get('url'):
                # The old client exports system-local naive dates. Convert with
                # Windows' local timezone rules, never silently treat them as UTC.
                due = datetime.fromisoformat(d['due']) if d.get('due') else None
                if due and due.tzinfo is None:
                    due = due.astimezone()
                items.append({'title': d['name'], 'course_key': entry.folder.split()[0],
                              'kind': 'quiz' if d.get('type') == 'quiz' else 'assignment',
                              'url': d['url'], 'due_at': due.isoformat() if due else None})
        reports.append({'source': 'moodle:deadlines', 'ok': True, 'items': items})
    except Exception:
        reports.append({'source': 'moodle:deadlines', 'ok': False, 'error': 'Moodle 截止日期读取失败。'})
    for entry in entries:
        key = entry.folder.split()[0]
        for source, course_id, dest in (('moodle', entry.moodle_id, entry.moodle_dest),
                                        ('artemis', entry.artemis_id, entry.artemis_dest)):
            if course_id is None:
                continue
            prefix = f'{source}-{course_id}'
            if sync:
                try:
                    report = call('sync_course' if source == 'moodle' else 'artemis_sync',
                                  course=str(course_id), dest_dir=dest, dry_run=False)
                    planner._write_json(run / f'{prefix}-sync.json', report)
                    imported = ingest(root, report, dest, source, key)
                    receipts.append({'source': source, 'course_id': course_id,
                                     **{k: report.get(k) for k in ('new', 'updated', 'unchanged', 'failed', 'local_modified')},
                                     'imported': imported, 'report': str(run / f'{prefix}-sync.json')})
                except Exception:
                    receipts.append({'source': source, 'course_id': course_id, 'error': '同步或材料登记未完成，原文件保留。'})
            try:
                if source == 'artemis':
                    rows = call('artemis_exercises', course=str(course_id))
                    items = [{'title': r['title'], 'course_key': key, 'kind': 'quiz' if r['type'] == 'quiz' else 'assignment',
                              'url': r['url'], 'due_at': r.get('due_date'),
                              'details': f"参与：{r.get('participation')}；得分：{r.get('score')}%；提交详情：接口未提供。"}
                             for r in rows]
                else:
                    rows = call('assignments', course=str(course_id))
                    items = [{'title': r['name'], 'course_key': key, 'kind': 'assignment',
                              'url': r['url'], 'raw_due': r.get('due'),
                              'completed': True if r.get('submission') == 'submitted' else None,
                              'details': f"提交：{r.get('submission')}；评分：{r.get('grading')}。" +
                              ' '.join(f"{s.get('label')}: {s.get('value')}" for s in r.get('status_rows', [])[:6])}
                             for r in rows if r.get('url')]
                planner._write_json(run / f'{prefix}-tasks.json', rows)
                partial = source == 'moodle' and any(r.get('available') and not r.get('fetched') for r in rows)
                reports.append({'source': f'{source}:{course_id}', 'ok': not partial, 'items': items,
                                'error': '部分 Moodle 作业页面读取失败，保留上次成功事项。'})
            except Exception:
                reports.append({'source': f'{source}:{course_id}', 'ok': False,
                                'error': '课程任务或主页读取失败；保留上次成功记录。'})
            if source == 'artemis':
                snapshot, report = _collect_artemis_forum(call, run, course_id, key)
                pages.append(snapshot)
                reports.append(report)
            if source == 'moodle':
                try:
                    activities = call('list_activities', course=str(course_id))
                    page = call('read_page', url=f'https://www.moodle.tum.de/course/view.php?id={course_id}')
                    planner._write_json(run / f'{prefix}-activities.json', activities)
                    planner._write_json(run / f'{prefix}-page.json', page)
                    paths = [str(run / f'{prefix}-page.json')]
                    seen = set()
                    for link in page.get('links', []):
                        address = link.get('url', '')
                        if '/mod/forum/view.php' not in address or not re.search(
                                r'announcement|news|ankündigung', link.get('text', ''), re.I) or address in seen:
                            continue
                        seen.add(address)
                        forum = call('read_page', url=address)
                        path = run / f'{prefix}-forum-{len(seen)}.json'
                        planner._write_json(path, forum)
                        paths.append(str(path))
                        discussions = dict()
                        for thread in forum.get('links', []):
                            parsed = urlsplit(thread.get('url', ''))
                            if parsed.path == '/mod/forum/discuss.php':
                                query = dict(parse_qsl(parsed.query))
                                if str(query.get('d', '')).isdigit():
                                    discussions.setdefault(query['d'], f'https://www.moodle.tum.de/mod/forum/discuss.php?d={query["d"]}')
                        for thread_id, address in list(discussions.items())[:8]:
                            content = call('read_page', url=address)
                            path = run / f'{prefix}-discussion-{thread_id}.json'
                            planner._write_json(path, content)
                            paths.append(str(path))
                    pages.append({'course_key': key, 'course_id': course_id, 'paths': paths,
                                  'coverage': '课程主页、公告首页、每公告区首页前8个不同讨论；更早公告及分页由人工核对。'})
                    reports.append({'source': f'moodle:pages-{course_id}', 'ok': True, 'items': []})
                except Exception:
                    reports.append({'source': f'moodle:pages-{course_id}', 'ok': False,
                                    'error': '课程主页或公告读取未完成，不能视为没有通知。'})
    result = update(root, reports)
    receipt = {'ok': all(r['ok'] for r in reports) and all(not r.get('error') and not (r.get('failed') or {}).get('count')
               for r in receipts), 'run_dir': str(run), 'sources': result['sources'], 'sync': receipts,
               'pages': pages, 'alert_count': len(result['alerts']), 'item_count': len(result['items'])}
    planner._write_json(run / 'receipt.json', receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description='课程检查、已有工具同步和本地提醒记录')
    parser.add_argument('--root', type=Path, default=planner.DEFAULT_DIR)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('collect')
    p.add_argument('--map', type=Path)
    p.add_argument('--sync', action='store_true')
    p = commands.add_parser('ingest', help='导入浏览器或页面核验后的规范化事项')
    p.add_argument('--input', type=Path, required=True)
    commands.add_parser('view')
    p = commands.add_parser('ack', help='仅在已向用户报告后确认提醒编号')
    p.add_argument('ids', nargs='+')
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        if args.command == 'collect':
            result = collect(args.root, args.map, sync=args.sync)
        elif args.command == 'ingest':
            result = update(args.root, json.loads(args.input.read_text(encoding='utf-8')))
        elif args.command == 'ack':
            result = acknowledge(args.root, args.ids)
        else:
            result = view(args.root)
    except Exception:
        print(json.dumps({'error': '检查未完成；已有记录和文件保留。请核对来源、输入格式或目录锁。'}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
