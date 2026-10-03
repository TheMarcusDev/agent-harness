"""Agent side of the Agent Harness. Talks to the local server (http://127.0.0.1:8765).

  python harness.py ask --agent Artist --title "Ship walk_cycle_v3?" \
         --kind review --media C:/path/a.mp4 C:/path/b.mp4 --body "..." --recommend Pass
  python harness.py ask --agent Designer --title "Shop location?" --kind choice \
         --options "East" "West" --recommend "East"
  python harness.py answers --agent Artist          # collect answered items (marks them delivered)
  python harness.py status --agent Artist --state working --task "rig export" --detail "18 clips"
  python harness.py event --agent Reviewer --kind gate --status ok --text "branch landed e047d13f"
  python harness.py withdraw --id 12
  python harness.py build --agent Builder --tree C:/src/myproject --config Debug Release --wait
  python harness.py build-status --id 7      # queue position, step, exit code, log tail
  python harness.py builds                   # the queue
  python harness.py build-cancel --id 7
  python harness.py run --agent Renderer --cwd C:/src/myproject --gpu --note "capture" \
         --cmd "python tests/run_captures.py" --wait   # captures/exe runs, FIFO gpu.lock

BUILDS RUN IN THE HARNESS, not in your shell: if your wait is killed, the build keeps going.
Re-attach with build-status (or build ... --wait on a new submit only if it really failed).

kind: yesno (Yes/No), choice (--options required), review (Pass/Fail unless --options), info (Seen).
Every command prints JSON. Exit code 1 if the server is not running.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:" + __import__("os").environ.get("HARNESS_PORT", "8765")


def call(path, payload=None):
    req = urllib.request.Request(BASE + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": e.code, "body": e.read().decode(errors="replace")}))
        sys.exit(1)
    except urllib.error.URLError as e:
        print(json.dumps({"error": f"harness server not running ({e.reason})"}))
        sys.exit(1)


def wait_build(jid):
    """Polls until the build finishes. Safe to lose: the build itself runs in the harness."""
    import time
    last = None
    while True:
        j = call(f"/api/build?id={jid}")
        state = (j["status"], j.get("step"), j.get("position"))
        if state != last:
            print(f"job #{jid}: {j['status']} step={j.get('step') or '-'} queue={j.get('position') or '-'}", flush=True)
            last = state
        if j["status"] in ("ok", "fail", "cancelled"):
            return {k: j[k] for k in ("id", "status", "exit_code", "log")} | {"logTail": j["logTail"][-2500:]}
        time.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    a = sub.add_parser("ask")
    a.add_argument("--agent", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--kind", default="yesno", choices=["yesno", "choice", "review", "info"])
    a.add_argument("--body", default="")
    a.add_argument("--options", nargs="*", default=None)
    a.add_argument("--media", nargs="*", default=[])
    a.add_argument("--recommend", default="")
    a.add_argument("--priority", type=int, default=1, choices=[0, 1, 2])
    n = sub.add_parser("answers")
    n.add_argument("--agent", required=True)
    n.add_argument("--peek", action="store_true", help="don't mark as delivered")
    s = sub.add_parser("status")
    s.add_argument("--agent", required=True)
    s.add_argument("--state", default="working", choices=["working", "blocked", "waiting", "idle"])
    s.add_argument("--task", default="")
    s.add_argument("--detail", default="")
    e = sub.add_parser("event")
    e.add_argument("--agent", required=True)
    e.add_argument("--text", required=True)
    e.add_argument("--kind", default="note", choices=["gate", "lock", "build", "note", "loop", "answer"])
    e.add_argument("--status", default="", choices=["", "ok", "fail", "run"])
    bd = sub.add_parser("build")
    bd.add_argument("--agent", required=True)
    bd.add_argument("--tree", required=True, help="worktree root holding CMakeLists.txt and build/")
    bd.add_argument("--config", nargs="+", default=["Debug"], choices=["Debug", "Release"])
    bd.add_argument("--target", default="")
    bd.add_argument("--configure", action="store_true", help="run cmake -S . -B build first")
    bd.add_argument("--note", default="")
    bd.add_argument("--build-dir", default="", help="default: the dir holding CMakeCache.txt (build-<suffix> or build)")
    bd.add_argument("--wait", action="store_true", help="block until done; prints the log tail")
    rn = sub.add_parser("run")
    rn.add_argument("--agent", required=True)
    rn.add_argument("--cwd", required=True)
    rn.add_argument("--cmd", required=True, help="bash command line")
    rn.add_argument("--gpu", action="store_true", help="wait in the FIFO gpu.lock queue first")
    rn.add_argument("--note", default="")
    rn.add_argument("--wait", action="store_true")
    bs = sub.add_parser("build-status")
    bs.add_argument("--id", type=int, required=True)
    bs.add_argument("--wait", action="store_true")
    sub.add_parser("builds")
    bc = sub.add_parser("build-cancel")
    bc.add_argument("--id", type=int, required=True)
    w = sub.add_parser("withdraw")
    w.add_argument("--id", type=int, required=True)
    g = sub.add_parser("get")
    g.add_argument("--id", type=int, required=True)
    args = ap.parse_args()

    if args.action == "ask":
        out = call("/api/ask", {"agent": args.agent, "title": args.title, "kind": args.kind, "body": args.body,
                                "options": args.options, "media": [m.replace("/", "\\") for m in args.media],
                                "recommend": args.recommend, "priority": args.priority})
    elif args.action == "answers":
        out = call(f"/api/answers?agent={urllib.request.quote(args.agent)}&mark={0 if args.peek else 1}")
        out = [{k: i[k] for k in ("id", "title", "answer", "note")} for i in out]
    elif args.action == "status":
        out = call("/api/status", {"agent": args.agent, "state": args.state, "task": args.task, "detail": args.detail})
    elif args.action == "event":
        out = call("/api/event", {"agent": args.agent, "text": args.text, "kind": args.kind, "status": args.status})
    elif args.action == "build":
        out = call("/api/build", {"agent": args.agent, "tree": args.tree, "configs": args.config,
                                  "target": args.target, "configure": args.configure, "note": args.note,
                                  "buildDir": args.build_dir})
        if args.wait:
            out = wait_build(out["id"])
    elif args.action == "run":
        out = call("/api/run", {"agent": args.agent, "cwd": args.cwd, "cmd": args.cmd,
                                "gpu": args.gpu, "note": args.note})
        if args.wait:
            out = wait_build(out["id"])
    elif args.action == "build-status":
        out = wait_build(args.id) if args.wait else call(f"/api/build?id={args.id}")
    elif args.action == "builds":
        out = [{k: j[k] for k in ("id", "agent", "status", "step", "position", "exit_code", "tree", "configs")}
               for j in call("/api/builds")]
    elif args.action == "build-cancel":
        out = call("/api/build/cancel", {"id": args.id})
    elif args.action == "withdraw":
        out = call("/api/withdraw", {"id": args.id})
    else:
        out = call(f"/api/item?id={args.id}")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
