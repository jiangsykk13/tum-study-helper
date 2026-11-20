"""Offline smoke test: start the MCP server over stdio and check which tools it offers.

No login and no network: listing tools never touches Moodle.

    python -I tests/mcp_smoke.py
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tum_moodle_auto.py"
EXPECTED_TOOLS = {"list_courses", "list_activities", "list_files", "read_page", "download", "upcoming_deadlines", "grades", "assignments", "sync_course",
                  "artemis_courses", "artemis_lectures", "artemis_exercises", "artemis_download", "artemis_sync",
                  "artemis_channels", "artemis_posts", "artemis_exercise_repos"}
# What ChatGPT gets through the tunnel: no tool that writes to disk. The repository
# URLs (artemis_exercise_repos) are only for the local web page for now.
EXPECTED_READ_ONLY_TOOLS = EXPECTED_TOOLS - {"download", "sync_course", "artemis_download", "artemis_sync",
                                             "artemis_exercise_repos"}


def main():
    check("mcp", EXPECTED_TOOLS)
    check("mcp-readonly", EXPECTED_READ_ONLY_TOOLS)


def check(command, expected):
    server = subprocess.Popen([sys.executable, "-I", "-B", str(SCRIPT), command], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def send(msg):
        server.stdin.write(json.dumps(msg) + "\n")
        server.stdin.flush()

    def recv():
        line = server.stdout.readline()
        if not line:
            sys.exit(f"Server exited early:\n{server.stderr.read()}")
        return json.loads(line)

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "smoke-test", "version": "0"}}})
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in recv()["result"]["tools"]}
    finally:
        server.stdin.close()
        server.wait(timeout=10)

    print(f"{command}:")
    for name, t in sorted(tools.items()):
        print(f"  {name}({', '.join(t['inputSchema'].get('properties', {}))})")
    missing, extra = expected - tools.keys(), tools.keys() - expected
    if missing or extra:
        sys.exit(f"FAIL ({command}): missing {sorted(missing)}, unexpected {sorted(extra)} "
                 "(update EXPECTED_TOOLS when you add a tool on purpose)")
    print(f"OK: {len(tools)} tools")


if __name__ == "__main__":
    main()
