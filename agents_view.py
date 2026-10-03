"""Agent list and read-only views for the web server.

Hosted agents (agenthost.py, :8767) are proxied. Agents still running in a terminal are listed from
`claude agents --json`, with their last activity taken from their transcript's modification time,
and their history shown read-only from that transcript until they migrate.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

AGENTHOST = "http://127.0.0.1:8767"
PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
_cache = {}


def _cached(key, secs, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < secs:
        return hit[1]
    try:
        v = fn()
    except Exception:
        v = hit[1] if hit else None
    _cache[key] = (time.time(), v)
    return v


def host(path, body=None, timeout=30):
    req = urllib.request.Request(AGENTHOST + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except urllib.error.URLError:
        return 503, {"error": "agent host is not running"}


def _transcript(cwd, session_id):
    try:
        from claude_agent_sdk import project_key_for_directory
        key = project_key_for_directory(cwd)
    except Exception:
        key = "".join(ch if ch.isalnum() else "-" for ch in cwd)
    return os.path.join(PROJECTS, key, f"{session_id}.jsonl")


def _terminal_sessions():
    out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return [a for a in json.loads(out or "[]") if a.get("kind") == "interactive"]


def agents():
    """Hosted + terminal agents, newest interaction first."""
    code, hosted = host("/agents", timeout=5)
    hosted = hosted if code == 200 and isinstance(hosted, list) else []
    names = {a["name"] for a in hosted}
    hosted_sids = {a.get("sessionId") for a in hosted}
    result = list(hosted)
    for t in _cached("terminals", 20, _terminal_sessions) or []:
        name = t.get("name") or t.get("sessionId", "")[:8]
        if name in names or t.get("sessionId") in hosted_sids:
            continue
        path = _transcript(t.get("cwd", ""), t.get("sessionId", ""))
        try:
            last = os.path.getmtime(path)
        except OSError:
            last = 0
        result.append({"name": name, "terminal": True, "state": "terminal", "enabled": True,
                       "sessionId": t.get("sessionId", ""), "cwd": t.get("cwd", ""), "pid": t.get("pid"),
                       "lastActivity": last, "color": ""})
        names.add(name)
    return sorted(result, key=lambda a: -(a.get("lastActivity") or 0))


def terminal_events(name, since=0, limit=300):
    """Read-only history of a terminal agent, shaped like agenthost events."""
    t = next((a for a in agents() if a["name"] == name and a.get("terminal")), None)
    if not t:
        return 404, {"error": "no such agent"}
    from claude_agent_sdk import get_session_messages
    evs, seq = [], 0
    try:
        msgs = get_session_messages(t["sessionId"], directory=t["cwd"])[-limit:]
    except Exception as e:
        msgs = []
        evs.append({"seq": 1, "type": "system", "text": f"history unavailable: {e}"})
    for m in msgs:
        body = m.message if isinstance(m.message, dict) else {}
        content = body.get("content")
        items = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for c in items:
            ty = c.get("type")
            if ty == "text":
                e = {"type": "user" if m.type == "user" else "assistant", "text": c.get("text", "")}
            elif ty == "tool_use":
                e = {"type": "tool_use", "id": c.get("id"), "name": c.get("name"), "input": json.dumps(c.get("input"))[:1500]}
            elif ty == "tool_result":
                cc = c.get("content")
                txt = cc if isinstance(cc, str) else "\n".join(x.get("text", "") for x in (cc or []) if isinstance(x, dict))
                e = {"type": "tool_result", "id": c.get("tool_use_id"), "text": (txt or "")[:1500], "error": bool(c.get("is_error"))}
            else:
                continue
            seq += 1
            e.update(seq=seq, history=True)
            evs.append(e)
    info = dict(t)
    info["seq"] = seq
    if since >= seq:
        time.sleep(15)      # nothing new can arrive from a static transcript view; slow the client's poll
        return 200, {"info": info, "events": []}
    return 200, {"info": info, "events": [e for e in evs if e["seq"] > since]}
