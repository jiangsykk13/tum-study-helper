"""Preview the real UI against fictional temporary plans. Never logs in.

python -I -B tests/preview_offline.py [--port 48123]
Close with Ctrl+C; all progress changes are confined to the system temp folder.
"""
from pathlib import Path
import argparse
import importlib.util
import sys
import json
import os
from datetime import timedelta

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("offline_web_fixture", Path(__file__).with_name("test_web.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def stress_plan(case):
    """85 fictional tasks, 73 carried; all files are below the test's temp directory."""
    import planner
    today = planner.berlin_now().date()
    day, earlier = today.isoformat(), (today - timedelta(days=1)).isoformat()
    courses = [('INHN0002', '编程基础'), ('INHN0004', '离散结构'), ('INHN0011', '数据库基础'),
               ('INHN0014', '数学分析'), ('INHN0001', '信息学导论'), ('INHN0003', '计算机结构'),
               ('CITHN2014', '生成式 AI 基础与应用'), ('MGTHN0133', '创业与家族企业')]
    materials = case.tmp / 'Fictional Course' / 'Official' / 'Slides'
    materials.mkdir(parents=True)
    notes = materials / '练习课件.pdf'
    notes.write_bytes(b'%PDF-1.4\n%fictional preview only\n')
    problem = case.tmp / 'Fictional Course' / 'Practice' / '旧题'
    problem.mkdir(parents=True)
    roots = []
    for i in range(73):
        key, name = courses[i % len(courses)]
        task = {'id': f'root{i}', 'title': f'{name}：第 {i + 1} 项旧任务', 'course_key': key, 'course': name,
                'section': 'course', 'kind': 'exercise', 'minutes': 45, 'required': True, 'origin': 'new',
                'detail': '先尝试完成练习，再对照课件检查。完成标准：写出过程，并记录一个还不理解的问题。'}
        if i == 0:
            task.update(title='旧 Java 题：班级名单', practice={'folder': str(problem), 'tests_total': 9, 'difficulty': 1})
        elif i == 1:
            task['materials'] = [{'path': str(notes), 'pages': [2, 4]}]
        roots.append(task)
    old_states = {d: {task['id']: {'state': 'done'} for task in planner.load_plan(case.planner_dir, d)['tasks']}
                  for d in planner.plan_dates(case.planner_dir)}
    planner._write_json(case.planner_dir / '进度.json', {'version': 1, 'days': old_states})
    planner._write_json(case.planner_dir / '计划' / f'{earlier}.json', {'version': 1, 'date': earlier, 'tasks': roots})
    fresh = []
    for i in range(10):
        key, name = courses[i % len(courses)]
        fresh.append({'id': f'new{i}', 'title': ('完成关系模型测验' if i == 0 else f'{name}：本周讲义与练习'),
                      'section': 'course', 'course_key': key, 'course': name, 'kind': 'deadline' if i == 0 else 'lecture',
                      'minutes': 40, 'required': True, 'origin': 'new',
                      'due_at': f'{day}T23:59:00{planner.berlin_now().strftime("%z")[:3]}:{planner.berlin_now().strftime("%z")[3:]}',
                      'detail': '阅读指定页段，独立写出答案，再回到这里记录完成情况。完成标准：三道题都有完整过程。',
                      'materials': [{'path': str(notes), 'pages': [1, 5]}]})
        if i != 0:
            fresh[-1].pop('due_at')
    for key, name, minutes in [('research', '科研', 80), ('extracurricular', '课外学习', 40)]:
        fresh.append({'id': key, 'title': name + '（内容自定）', 'section': 'research', 'course_key': key, 'course': name,
                      'minutes': minutes, 'required': False, 'origin': 'new'})
    carried = [{'id': f'carry{i}', 'title': t['title'], 'detail': t['detail'], 'course_key': t['course_key'], 'course': t['course'],
                'section': 'course', 'kind': t['kind'], 'minutes': 45, 'required': True, 'origin': 'debt', 'auto_carry': True,
                'debt_from': earlier, 'carry_of': f'{earlier}/{t["id"]}', 'work_span': {'start': 0, 'end': 2700}} for i, t in enumerate(roots)]
    now = planner.berlin_now().isoformat(timespec='seconds')
    plan = {'version': 1, 'date': day, 'run_id': 'fictional-preview', 'generated_at': now, 'targets': {'course_hours': '6–8'},
            'tasks': fresh + carried, 'notices': ['今天先完成关系模型测验，再做本周课程任务。', '明天习题课前，整理一道没弄懂的问题。',
                                                '这条是可折叠提醒，用来验证保存打勾后仍保持展开。', '顺延任务全部保留，按需要展开。'],
            'upcoming': [{'date': earlier, 'course': '数据库基础', 'title': '旧测验（虚构）'},
                         {'date': day, 'course': '编程基础', 'title': '今天的练习截止（虚构）', 'time': '23:59'},
                         {'date': (today + timedelta(days=2)).isoformat(), 'course': '离散结构', 'title': '短测报名（虚构）'}]}
    planner._write_json(case.planner_dir / '计划' / f'{day}.json', plan)
    planner._write_json(case.planner_dir / '状态.json', {'ok': True, 'state': 'published', 'plan_date': day, 'run_id': 'fictional-preview', 'run_at': now})
    planner._write_json(case.planner_dir / '练习记录.json', {'version': 1, 'problems': [{'folder': str(problem), 'skeleton_sha256': 'framework',
                       'last_result': {'code_sha256': 'framework', 'passed': 0, 'total': 9, 'checked_at': now}}]})


def interrupted_plan(case):
    import planner
    now = planner.berlin_now()
    day, old = now.date().isoformat(), (now - timedelta(hours=2)).isoformat(timespec='seconds')
    formal = case.planner_dir / '计划' / f'{day}.json'
    plan = json.loads(formal.read_text(encoding='utf-8'))
    draft, receipt = case.planner_dir / '草稿' / formal.name, case.planner_dir / '运行' / formal.name
    planner._write_json(draft, plan)
    planner._write_json(receipt, {'version': 1, 'target_date': day, 'run_id': plan['run_id'], 'published': False,
                       'updated_at': old, 'stages': {'practice': {'ok': True, 'at': old}}})
    for path in (draft, receipt):
        seconds = (now - timedelta(hours=2)).timestamp()
        os.utime(path, (seconds, seconds))
    formal.unlink()  # Only the fictional test file inside case.tmp.
    planner._write_json(case.planner_dir / '状态.json', {'ok': None, 'state': 'running', 'plan_date': day,
                       'run_id': plan['run_id'], 'run_at': old})


def artemis_demo(case):
    """Fictional Artemis exercises with clone URLs for the exercise page."""
    git = "https://artemis.tum.de/git/DEMO26{0}/demo26{1}{2}-ab12cde.git"
    def repos(key, practice=True, graded=True):
        return {"practice": git.format(key.upper(), key.lower(), "-practice") if practice else None,
                "graded": git.format(key.upper(), key.lower(), "") if graded else None}
    rows = [
        ("H01E01 - Fictional Course Participant", "programming", "closed", "2026-10-29T22:59:00Z", 100.0, repos("h01e01")),
        ("H01E02 - Fictional Library", "programming", "closed", "2026-10-29T22:59:00Z", 100.0, repos("h01e02", practice=False)),
        ("L01E01 - Fictional Quiz", "quiz", "closed", "2026-10-22T14:00:00Z", 20.0, None),
        ("T03E02 - Fictional Queue Express", "programming", "closed", "2026-11-05T19:00:00Z", None, repos("t03e02", practice=False)),
        ("H04E01 - Fictional Bank With A Rather Long Title To Test Wrapping", "programming", "open", "2026-11-19T22:59:00Z", None, repos("h04e01", practice=False, graded=False)),
        ("T04E03 - Fictional Modelling", "modeling", "open", "2026-11-15T22:59:00Z", None, None),
        ("P04E04 - Fictional Practice Inheritance", "programming", "open", None, None, repos("p04e04", practice=False)),
    ]
    RELEASES = ["2026-10-22T12:50:00Z", "2026-10-29T13:00:00Z", "2026-10-22T12:00:00Z", "2026-11-05T14:00:00Z",
                "2026-11-12T12:00:00Z", "2026-11-11T19:00:00Z", None]
    exercises = []
    for index, (title, kind, status, due, score, repositories) in enumerate(rows):
        exercises.append({"id": 9000 + index, "course_id": 901, "type": kind, "title": title, "status": status,
                          "due_date": due, "release_date": RELEASES[index], "counts": "bonus" if title.startswith("P") else "graded",
                          "difficulty": ["EASY", "MEDIUM", "HARD"][index % 3], "categories": ["Loops"] if index == 3 else [],
                          "score": score, "practice_score": None,
                          "url": f"https://artemis.tum.de/courses/901/exercises/{9000 + index}",
                          "repositories": repositories or {"practice": None, "graded": None}})
    case.results["artemis_courses"] = [{"id": 901, "title": "Fictional Programming 26", "short_name": "demo26"},
                                       {"id": 902, "title": "Fictional Architecture 26", "short_name": "arch26"}]
    case.results["artemis_exercise_repos"] = exercises


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=fixture.PORT)
    parser.add_argument('--stress', action='store_true', help='85 tasks including 73 carried tasks, in temp files only')
    parser.add_argument('--interrupted', action='store_true', help='With --stress: missing today, stale receipt, unpublished draft')
    args = parser.parse_args()
    if args.interrupted and not args.stress:
        parser.error('--interrupted requires --stress')
    case = fixture.WebTest()
    case.setUp()
    artemis_demo(case)
    if args.stress:
        stress_plan(case)
    if args.interrupted:
        interrupted_plan(case)
    import uvicorn
    try:
        app = fixture.web.create_app(case.tools, token=fixture.TOKEN, port=args.port,
                                     map_path=case.map_path, opener=case.opened.append, planner_dir=case.planner_dir)
        print(f"Offline fictional preview: http://127.0.0.1:{args.port}/#t={fixture.TOKEN}", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
