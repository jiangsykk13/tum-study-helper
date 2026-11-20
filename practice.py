"""Compile one Java practice problem and run its JUnit 5 tests, like Artemis does.

    python -I practice.py "<problem folder>"              # the user's code in src\\
    python -I practice.py "<problem folder>" --solution   # the reference answer in 答案\\
    python -I practice.py "<problem folder>" --init-runner  # add 运行测试.bat (double-click to test)

A problem folder holds src\\*.java (the skeleton the user completes), test\\*Test.java
and 答案\\*.java (same class names). Everything is compiled into a temporary folder,
so the problem folder is never written. Nothing here uses the network.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path

JUNIT_VERSION = "1.11.4"
PLANNER_DIR = Path(os.environ.get("TUM_STUDY_ROOT") or Path(__file__).resolve().parent.parent) / "Planner"
JUNIT = PLANNER_DIR / "lib" / f"junit-platform-console-standalone-{JUNIT_VERSION}.jar"
JAVA_RELEASE = "17"
TIMEOUT = 120


def code_fingerprint(folder, subdir="src") -> str:
    """Bind assessments to a particular answer *and* test suite, not a score."""
    h = hashlib.sha256()
    for name in (subdir, "test"):
        for path in sorted((Path(folder) / name).glob("*.java")):
            h.update(f"{name}/{path.name}\0".encode("utf-8"))
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _tail(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def count_tests(folder) -> int:
    return sum(len(re.findall(r"@Test\b", p.read_text(encoding="utf-8", errors="replace")))
               for p in sorted((Path(folder) / "test").glob("*.java")))


def run_tests(folder, solution: bool = False, junit: Path = JUNIT, timeout: int = TIMEOUT) -> dict:
    """{compiled, total, passed, failed: [test names], error?, checked_at}; never raises for user mistakes."""
    folder = Path(folder)
    code = sorted((folder / ("答案" if solution else "src")).glob("*.java"))
    tests = sorted((folder / "test").glob("*.java"))
    result = {"folder": str(folder), "solution": solution, "compiled": False, "total": count_tests(folder),
              "passed": 0, "failed": [], "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if not code or not tests:
        result["error"] = "缺少 src（或 答案）里的 .java 文件，或 test 里的测试。"
        return result
    result["code_sha256"] = code_fingerprint(folder, "答案" if solution else "src")
    if not Path(junit).is_file():
        result["error"] = f"没有找到 JUnit：{junit}"
        return result
    with tempfile.TemporaryDirectory(prefix="practice-") as tmp:
        main_out, test_out, reports = (Path(tmp) / name for name in ("main", "test", "reports"))
        steps = [
            ["javac", "--release", JAVA_RELEASE, "-encoding", "UTF-8", "-d", str(main_out), *map(str, code)],
            ["javac", "--release", JAVA_RELEASE, "-encoding", "UTF-8", "-d", str(test_out),
             "-cp", os.pathsep.join([str(main_out), str(junit)]), *map(str, tests)],
        ]
        for command in steps:
            try:
                done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["error"] = f"编译没有完成：{type(exc).__name__}"
                return result
            if done.returncode != 0:
                result["error"] = "编译失败：\n" + _tail(done.stderr or done.stdout)
                return result
        result["compiled"] = True
        command = ["java", "-jar", str(junit), "execute", "--disable-banner", "--details=none",
                   "--class-path", os.pathsep.join([str(main_out), str(test_out)]), "--scan-class-path",
                   "--reports-dir", str(reports)]
        try:
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"测试没有跑完（可能有死循环）：{type(exc).__name__}"
            return result
        cases = []
        for report in reports.glob("*.xml"):
            try:
                cases.extend(ElementTree.parse(report).getroot().iter("testcase"))
            except ElementTree.ParseError:
                continue
        if not cases:
            result["error"] = "没有拿到测试结果：\n" + _tail(done.stdout + done.stderr)
            return result
        failed = [case.get("name", "?") for case in cases
                  if case.find("failure") is not None or case.find("error") is not None]
        skipped = [case.get("name", "?") for case in cases if case.find("skipped") is not None]
        result.update(total=len(cases), passed=len(cases) - len(failed) - len(skipped), failed=failed, skipped=skipped)
        if skipped:
            result["error"] = "部分测试未执行，请核对被禁用或中止的测试。"
    return result


RUNNER_NAME = "运行测试.bat"


def _records(root):
    path = Path(root) / "练习记录.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "problems": []}
    except (ValueError, OSError):
        raise ValueError("练习记录损坏，已停止写入。") from None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("problems"), list):
        raise ValueError("练习记录格式不认识，已停止写入。")
    if any(not isinstance(p, dict) or not isinstance(p.get("folder"), str) or not p["folder"]
           or not isinstance(p.get("date"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["date"])
           for p in data["problems"]):
        raise ValueError("练习记录包含无效题目，已停止写入。")
    return data


def _local_helpers():
    parent = str(Path(__file__).resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from local_lock import directory_lock
    from planner import _write_json
    return directory_lock, _write_json


def register_problem(root, folder, *, day, title, topic, difficulty=1, junit=JUNIT):
    """Validate a generated problem once; a target day has one stable identity."""
    from datetime import date
    date.fromisoformat(day)
    if difficulty not in (1, 2, 3) or not title or not topic:
        raise ValueError("题目名称、知识点或难度无效。")
    folder = Path(folder).resolve()
    lock, write = _local_helpers()
    with lock(root):
        data = _records(root)
        found = next((p for p in data["problems"] if p.get("date") == day), None)
        if found:
            if Path(found["folder"]).resolve() != folder:
                raise ValueError("这一天已有练习，请复用原文件夹，不再创建第二道。")
            return {"ok": True, "reused": True, "problem": found}
    solution, skeleton = run_tests(folder, True, junit), run_tests(folder, False, junit)
    total = skeleton.get("total", 0)
    if (not solution.get("compiled") or solution.get("error") or solution.get("passed") != solution.get("total")
            or solution.get("total") != total or solution.get("skipped") or skeleton.get("skipped")
            or not 6 <= total <= 10 or not skeleton.get("compiled") or skeleton.get("error")
            or skeleton.get("passed", 0) * 2 >= total):
        raise ValueError("题目未通过质量检查：答案须全过，框架须可编译且多数测试失败，测试数须为6至10。")
    item = {"identity": f"java-{day}", "date": day, "folder": str(folder), "title": title, "topic": topic,
            "difficulty": difficulty, "next_difficulty": difficulty, "tests_total": total,
            "skeleton_sha256": skeleton["code_sha256"], "last_result": None, "assessments": []}
    with lock(root):
        data = _records(root)
        if any(p.get("date") == day for p in data["problems"]):
            raise ValueError("练习记录已改变，请重新读取后复用。")
        data["problems"].append(item)
        write(Path(root) / "练习记录.json", data)
    return {"ok": True, "reused": False, "problem": item}


def record_result(root, folder, *, junit=JUNIT):
    """Only a new code/test fingerprint can adjust the next difficulty once."""
    folder = Path(folder).resolve()
    lock, write = _local_helpers()
    with lock(root):
        data = _records(root)
        item = next((p for p in data["problems"] if Path(p["folder"]).resolve() == folder), None)
        if item is None:
            raise ValueError("练习未登记，请先登记题目与框架指纹。")
        baseline = item.get("skeleton_sha256")
    result = run_tests(folder, False, junit)
    fingerprint = result.get("code_sha256")
    with lock(root):
        data = _records(root)
        item = next(p for p in data["problems"] if Path(p["folder"]).resolve() == folder)
        seen = {a["code_sha256"] for a in item.get("assessments", [])}
        changed = bool(baseline and fingerprint and fingerprint != baseline)
        adjust = bool(changed and fingerprint not in seen and result.get("total", 0) > 0 and not result.get("error"))
        # A zero-pass *attempt* differs from the untouched framework by hash.
        if adjust:
            level = int(item.get("next_difficulty", item.get("difficulty", 1)))
            ratio = result["passed"] / result["total"]
            item["next_difficulty"] = min(3, level + 1) if ratio == 1 else max(1, level - 1) if ratio < .6 else level
        if fingerprint and fingerprint not in seen:
            item.setdefault("assessments", []).append({"code_sha256": fingerprint, "checked_at": result["checked_at"],
                                                       "passed": result["passed"], "total": result["total"],
                                                       "adjusted": adjust, "attempted": changed})
        item["last_result"] = result
        write(Path(root) / "练习记录.json", data)
    return {**result, "attempted": changed, "difficulty_adjusted": adjust,
            "baseline_missing": baseline is None, "next_difficulty": item.get("next_difficulty", item.get("difficulty", 1))}


def write_runner(folder) -> Path:
    """The double-click test launcher; CRLF and ASCII only (cmd.exe), never overwrites."""
    script = Path(__file__).resolve()
    lines = ["@echo off", f'"{sys.executable}" -I -B "{script}" "%~dp0."', "pause", ""]
    target = Path(folder) / RUNNER_NAME
    with target.open("x", encoding="ascii", newline="") as handle:
        handle.write("\r\n".join(lines))
    return target


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(prog="practice.py", description="编译并测试一道 Java 练习题")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--solution", action="store_true", help="测试 答案\\ 里的参考答案")
    parser.add_argument("--junit", type=Path, default=JUNIT)
    parser.add_argument("--init-runner", action="store_true", help=f"在文件夹里生成 {RUNNER_NAME}（不覆盖）")
    parser.add_argument("--record", action="store_true", help="保存检测结果；相同代码只调整一次难度")
    parser.add_argument("--register", action="store_true", help="质量检查后登记新题及框架指纹")
    parser.add_argument("--root", type=Path, default=PLANNER_DIR)
    parser.add_argument("--date")
    parser.add_argument("--title")
    parser.add_argument("--topic")
    parser.add_argument("--difficulty", type=int, default=1)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.init_runner:
        try:
            print(json.dumps({"written": str(write_runner(args.folder))}, ensure_ascii=False))
            return 0
        except (OSError, UnicodeEncodeError) as exc:
            print(json.dumps({"error": f"没有生成 {RUNNER_NAME}：{type(exc).__name__}"}, ensure_ascii=False))
            return 1
    if args.register or args.record:
        try:
            if args.register:
                if not all((args.date, args.title, args.topic)):
                    raise ValueError("登记需要 --date、--title、--topic。")
                result = register_problem(args.root, args.folder, day=args.date, title=args.title,
                                          topic=args.topic, difficulty=args.difficulty, junit=args.junit)
            else:
                result = record_result(args.root, args.folder, junit=args.junit)
        except (ValueError, OSError, RuntimeError):
            result = {"error": "练习记录操作未完成；请核对参数、质量检查和现有记录，原记录已保留。"}
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1 if "error" in result else 0
    result = run_tests(args.folder, args.solution, args.junit)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["compiled"] and not result["failed"] and "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
