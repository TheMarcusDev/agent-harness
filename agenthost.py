"""Agent Harness agent host: runs Claude Code agent sessions without terminals.

Each agent is a Claude Agent SDK client (ClaudeSDKClient) resuming its EXISTING session id, so its
whole history, memory and CLAUDE.md come with it; a new agent starts a fresh session. Permission
prompts become Allow/Deny cards in the agent's chat window (and keep the agent waiting meanwhile).

Separate, long-lived process: neither the web server nor the job host restarts it, so a dashboard
update never interrupts an agent. It listens on 127.0.0.1:8767 only; server.py proxies to it.

Registry: agents.json  {name: {sessionId, cwd, color, enabled, created}}
"""

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quota  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny,
    RateLimitEvent, ResultMessage, SystemMessage, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock,
    UserMessage, get_session_messages)

REGISTRY = os.path.join(HERE, "agents.json")
PORT = 8767
MAX_EVENTS = 3000
LOOP = asyncio.new_event_loop()
_reg_lock = threading.Lock()


def load_registry():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_registry(reg):
    with _reg_lock:
        tmp = REGISTRY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=1)
        os.replace(tmp, REGISTRY)


def _short(v, n=4000):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"\n… ({len(s) - n} more chars)"


def _tool_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    out.append(c.get("text", ""))
                elif c.get("type") == "image":
                    out.append("[image]")
            else:
                out.append(str(c))
        return "\n".join(out)
    return "" if content is None else str(content)


class Agent:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.events = []          # [{seq, t, type, ...}]
        self.seq = 0
        self.state = "stopped"    # stopped | starting | idle | busy | permission | error
        self.error = ""
        self.last_activity = cfg.get("lastActivity", 0)
        self.client = None
        self.pending = {}         # perm id -> (future, info)
        self.cond = threading.Condition()

    # ---- event log (read by HTTP threads, written by the loop thread)
    def emit(self, type_, **kw):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "type": type_, **kw}
            self.events.append(ev)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            self.cond.notify_all()
        return ev

    def touch(self):
        self.last_activity = time.time()
        reg = load_registry()
        if self.name in reg:
            reg[self.name]["lastActivity"] = self.last_activity
            if self.cfg.get("sessionId"):
                reg[self.name]["sessionId"] = self.cfg["sessionId"]
            save_registry(reg)

    def info(self):
        return {"name": self.name, "color": self.cfg.get("color", "#8a8a93"), "state": self.state,
                "enabled": self.cfg.get("enabled", True), "sessionId": self.cfg.get("sessionId", ""),
                "cwd": self.cfg.get("cwd", ""), "lastActivity": self.last_activity, "error": self.error,
                "pendingPermissions": len(self.pending), "seq": self.seq,
                "chrome": bool(self.cfg.get("chrome"))}

    # ---- history from the session transcript
    def load_history(self, limit=400):
        sid = self.cfg.get("sessionId")
        if not sid:
            return
        try:
            msgs = get_session_messages(sid, directory=self.cfg.get("cwd"))
        except Exception as e:
            self.emit("system", text=f"history unavailable: {e}")
            return
        for m in msgs[-limit:]:
            body = m.message if isinstance(m.message, dict) else {}
            content = body.get("content")
            if m.type == "user":
                if isinstance(content, str):
                    self.emit("user", text=content, history=True)
                elif isinstance(content, list):
                    for c in content:
                        if c.get("type") == "text":
                            self.emit("user", text=c.get("text", ""), history=True)
                        elif c.get("type") == "tool_result":
                            self.emit("tool_result", id=c.get("tool_use_id"), text=_short(_tool_result_text(c.get("content")), 1500),
                                      error=bool(c.get("is_error")), history=True)
            elif m.type == "assistant" and isinstance(content, list):
                for c in content:
                    if c.get("type") == "text":
                        self.emit("assistant", text=c.get("text", ""), history=True)
                    elif c.get("type") == "tool_use":
                        self.emit("tool_use", id=c.get("id"), name=c.get("name"), input=_short(c.get("input"), 1500), history=True)
        self.emit("system", text=f"— resumed session {sid[:8]} ({len(msgs)} messages on disk) —")

    # ---- lifecycle (loop thread)
    async def start(self):
        if self.client:
            return
        self.state, self.error = "starting", ""
        if not self.events:
            self.load_history()
        opts = ClaudeAgentOptions(
            cwd=self.cfg.get("cwd") or None,
            resume=self.cfg.get("sessionId") or None,
            setting_sources=["user", "project", "local"],
            can_use_tool=self._can_use_tool,
            # `--chrome` is how a terminal session got Claude in Chrome; per agent, from the registry.
            extra_args={"name": self.name, **({"chrome": None} if self.cfg.get("chrome") else {})},
            # Screenshots and big tool results arrive as single JSON lines; the 1 MB default
            # ended an agent's stream ("JSON message exceeded maximum buffer size").
            max_buffer_size=256 * 1024 * 1024,
        )
        try:
            self.client = ClaudeSDKClient(options=opts)
            await self.client.connect()
            self.state = "idle"
            asyncio.ensure_future(self._receive())
            self.emit("system", text="agent online")
        except Exception as e:
            self.client = None
            self.state, self.error = "error", str(e)[:300]
            self.emit("error", text=f"start failed: {e}")

    async def stop(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        for fut, _ in list(self.pending.values()):
            if not fut.done():
                fut.set_result(PermissionResultDeny(message="agent stopped", interrupt=True))
        self.pending.clear()
        self.state = "stopped"
        self.emit("system", text="agent stopped")

    async def send(self, text):
        if not self.client:
            await self.start()
        # A message sent while the session is still connecting was lost (the first nudge after a host
        # restart reached 1 of 10 agents): wait until the session is online.
        for _ in range(600):
            if self.state != "starting":
                break
            await asyncio.sleep(0.1)
        if not self.client:
            return
        self.emit("user", text=text)
        self.state = "busy"
        self.touch()
        await self.client.query(text)

    async def interrupt(self):
        # A turn parked on a permission card cannot end until the card is answered, so deny it first.
        for fut, _ in list(self.pending.values()):
            if not fut.done():
                fut.set_result(PermissionResultDeny(message="interrupted by the owner", interrupt=True))
        self.pending.clear()
        if self.client:
            await self.client.interrupt()
            self.emit("system", text="interrupted")

    async def _can_use_tool(self, tool_name, tool_input, context):
        pid = f"p{int(time.time() * 1000)}"
        fut = LOOP.create_future()
        info = {"id": pid, "tool": tool_name, "input": _short(tool_input, 2500),
                "title": getattr(context, "title", None) or "", "description": getattr(context, "description", None) or "",
                "suggestions": bool(getattr(context, "suggestions", None))}
        self.pending[pid] = (fut, context)
        self.state = "permission"
        self.emit("permission", **info)
        self.touch()
        result = await fut
        self.pending.pop(pid, None)
        self.state = "busy"
        return result

    def decide(self, pid, allow, always=False, message=""):
        entry = self.pending.get(pid)
        if not entry:
            return False
        fut, context = entry
        if allow:
            perms = getattr(context, "suggestions", None) if always else None
            res = PermissionResultAllow(updated_permissions=perms or None)
        else:
            res = PermissionResultDeny(message=message or "Denied by the owner in the harness")
        LOOP.call_soon_threadsafe(lambda: fut.done() or fut.set_result(res))
        self.emit("permission_decided", id=pid, allow=allow, always=always)
        return True

    async def _receive(self):
        try:
            async for msg in self.client.receive_messages():
                self._handle(msg)
        except Exception as e:
            self.state, self.error = "error", str(e)[:300]
            self.emit("error", text=f"stream ended: {e}")
            self.client = None
            # Come back on our own: the session is on disk, so a resume loses nothing but the
            # turn in flight. Without this an agent stays dead until someone clicks restart.
            if self.cfg.get("enabled", True):
                await asyncio.sleep(5)
                self.emit("system", text="restarting after the stream error")
                await self.start()

    def _handle(self, msg):
        if isinstance(msg, AssistantMessage):
            self.state = "busy" if self.state != "permission" else self.state
            for b in msg.content:
                if isinstance(b, TextBlock):
                    self.emit("assistant", text=b.text)
                elif isinstance(b, ThinkingBlock):
                    self.emit("thinking", text=_short(b.thinking, 3000))
                elif isinstance(b, ToolUseBlock):
                    self.emit("tool_use", id=b.id, name=b.name, input=_short(b.input, 2500))
        elif isinstance(msg, UserMessage):
            origin = getattr(msg, "origin", None)
            kind = getattr(origin, "kind", None) if origin else None
            if isinstance(msg.content, str):
                self.emit("user", text=msg.content, origin=kind or "")
            else:
                for b in msg.content:
                    if isinstance(b, ToolResultBlock):
                        self.emit("tool_result", id=b.tool_use_id, text=_short(_tool_result_text(b.content), 2500),
                                  error=bool(b.is_error))
                    elif isinstance(b, TextBlock):
                        self.emit("user", text=b.text, origin=kind or "")
            if kind and kind != "human":
                self.touch()
        elif isinstance(msg, ResultMessage):
            if msg.session_id and msg.session_id != self.cfg.get("sessionId"):
                self.cfg["sessionId"] = msg.session_id
            self.state = "idle"
            self.emit("result", cost=msg.total_cost_usd, turns=msg.num_turns, error=bool(msg.is_error),
                      text=(msg.result or "")[:300] if msg.is_error else "")
            self.touch()
        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                sid = (msg.data or {}).get("session_id")
                if sid and sid != self.cfg.get("sessionId"):
                    self.cfg["sessionId"] = sid
                    self.touch()
        elif isinstance(msg, RateLimitEvent):
            quota.update(msg.rate_limit_info.raw)


AGENTS = {}


def run(coro, timeout=60):
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


def boot():
    for name, cfg in load_registry().items():
        AGENTS[name] = Agent(name, cfg)
        if cfg.get("enabled", True) and cfg.get("autostart", True):
            asyncio.run_coroutine_threadsafe(AGENTS[name].start(), LOOP)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if parts == ["agents"]:
            return self._send(200, sorted((a.info() for a in AGENTS.values()), key=lambda i: -i["lastActivity"]))
        if len(parts) == 3 and parts[0] == "agent" and parts[2] == "events":
            a = AGENTS.get(parts[1])
            if not a:
                return self._send(404, {"error": "no such agent"})
            since = int(q.get("since", ["0"])[0])
            wait = min(25.0, float(q.get("wait", ["0"])[0]))
            deadline = time.time() + wait
            with a.cond:
                while a.seq <= since and time.time() < deadline:
                    a.cond.wait(deadline - time.time())
                evs = [e for e in a.events if e["seq"] > since]
            return self._send(200, {"info": a.info(), "events": evs})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            b = self._body()
        except ValueError:
            return self._send(400, {"error": "bad json"})
        try:
            if parts == ["agents"]:
                name = (b.get("name") or "").strip()
                if not name or not name.replace("-", "").replace("_", "").isalnum():
                    return self._send(400, {"error": "name: letters, digits, - and _ only"})
                reg = load_registry()
                if name in reg:
                    return self._send(409, {"error": "exists"})
                reg[name] = {"sessionId": b.get("sessionId", ""), "cwd": b.get("cwd") or __import__("config").CFG["defaultCwd"],
                             "color": b.get("color") or "#8a8a93", "enabled": True, "created": time.time(),
                             "lastActivity": time.time()}
                save_registry(reg)
                AGENTS[name] = Agent(name, reg[name])
                run(AGENTS[name].start())
                return self._send(200, AGENTS[name].info())
            if len(parts) == 3 and parts[0] == "agent":
                a = AGENTS.get(parts[1])
                if not a:
                    return self._send(404, {"error": "no such agent"})
                act = parts[2]
                if act == "send":
                    text = (b.get("text") or "").strip()
                    if not text:
                        return self._send(400, {"error": "empty"})
                    asyncio.run_coroutine_threadsafe(a.send(text), LOOP)
                    return self._send(200, {"ok": True})
                if act == "interrupt":
                    run(a.interrupt())
                    return self._send(200, {"ok": True})
                if act == "permission":
                    ok = a.decide(b.get("id"), bool(b.get("allow")), bool(b.get("always")), b.get("message", ""))
                    return self._send(200 if ok else 404, {"ok": ok})
                if act in ("enable", "disable"):
                    reg = load_registry()
                    reg.setdefault(a.name, a.cfg)["enabled"] = act == "enable"
                    a.cfg["enabled"] = act == "enable"
                    save_registry(reg)
                    run(a.start() if act == "enable" else a.stop())
                    return self._send(200, a.info())
                if act == "remove":
                    run(a.stop())
                    reg = load_registry()
                    reg.pop(a.name, None)
                    save_registry(reg)
                    AGENTS.pop(a.name, None)
                    return self._send(200, {"ok": True, "removed": a.name})
                if act == "chrome":
                    # Claude in Chrome on/off for this agent; takes effect by restarting its session.
                    reg = load_registry()
                    reg.setdefault(a.name, a.cfg)["chrome"] = bool(b.get("on"))
                    a.cfg["chrome"] = bool(b.get("on"))
                    save_registry(reg)
                    run(a.stop())
                    run(a.start())
                    return self._send(200, a.info())
                if act == "restart":
                    run(a.stop())
                    run(a.start())
                    return self._send(200, a.info())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)   # also the singleton guard
    srv.daemon_threads = True
    threading.Thread(target=LOOP.run_forever, daemon=True).start()
    boot()
    srv.serve_forever()


if __name__ == "__main__":
    main()
