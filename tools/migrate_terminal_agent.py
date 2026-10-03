"""Move one Claude Code terminal session into the harness: end its terminal session (only between turns), then
resume the same session id in agenthost. Usage: python tools/migrate_terminal_agent.py <Name> <"#rrggbb">
The session must have been started with --name <Name> (it is found by name in `claude agents --json`)."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import os
B = "http://127.0.0.1:" + os.environ.get("HARNESS_PORT", "8765")
name, color = sys.argv[1], sys.argv[2]
agents = json.loads(subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True).stdout)
t = next(a for a in agents if a.get("kind") == "interactive" and a.get("name") == name)
print(f"{name}: pid {t['pid']} session {t['sessionId'][:8]}")
subprocess.run(["taskkill", "/F", "/T", "/PID", str(t["pid"])], capture_output=True)
time.sleep(2)
req = urllib.request.Request(B + "/api/agents", data=json.dumps(
    {"name": name, "sessionId": t["sessionId"], "cwd": t["cwd"], "color": color}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    info = json.loads(urllib.request.urlopen(req, timeout=180).read())
except urllib.error.HTTPError as e:
    print("FAILED", e.code, e.read().decode())
    sys.exit(1)
time.sleep(2)
ev = json.loads(urllib.request.urlopen(f"{B}/api/agent/{name}/events?since=0", timeout=60).read())
hist = sum(1 for e in ev["events"] if e.get("history"))
print(f"state={ev['info']['state']} session={ev['info']['sessionId'][:8]} history_events={hist}")
