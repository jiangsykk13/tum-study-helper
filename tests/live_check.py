"""Explicit live validation in a disposable directory, never a real Official.

python -I -B tests/live_check.py --course ID [--artemis] [--sync]
Default: read-only preview. --sync downloads only to system temp, checks a second
preview and local hashes, and removes its own temporary directory on exit.
No credentials or raw website error messages are printed.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", required=True, type=int)
    parser.add_argument("--artemis", action="store_true")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    import tum_moodle_auto as app
    client = app.make_artemis_client() if args.artemis else app.make_client()
    saved_mkdir = os.mkdir
    with patch("tempfile._os.mkdir", side_effect=lambda path, mode=0o777, **kw: saved_mkdir(path, 0o777, **kw)):
        tmp = tempfile.TemporaryDirectory(prefix="tum-live-check-")
    try:
        target = Path(tmp.name) / "Official"
        first = client.sync_course(str(args.course), str(target), dry_run=True)
        output = {"source": "artemis" if args.artemis else "moodle", "course_id": args.course,
                  "preview": {k: first[k]["count"] for k in ("new", "updated", "unchanged", "failed")},
                  "preview_zero_write": not target.exists(),
                  "coverage": {status: sum(c.get("status") == status for c in first.get("coverage", []))
                               for status in {c.get("status") for c in first.get("coverage", [])}}}
        if args.sync:
            synced = client.sync_course(str(args.course), str(target), dry_run=False)
            verified = 0
            for item in synced.get("artifacts", []):
                path = target / item["local_path"]
                h = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != item["sha256"]:
                    raise ValueError("digest mismatch")
                verified += 1
            second = client.sync_course(str(args.course), str(target), dry_run=True)
            output.update(downloaded={k: synced[k]["count"] for k in ("new", "updated", "unchanged", "failed")},
                          verified_hashes=verified,
                          second_preview={k: second[k]["count"] for k in ("new", "updated", "unchanged", "failed")})
        print(json.dumps(output, ensure_ascii=False, indent=1))
        return 0 if output["preview_zero_write"] and not first["failed"]["count"] else 1
    except Exception as exc:
        print(json.dumps({"error": "真实验证未完成；未改动用户课件目录，也未输出登录信息。", "type": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
