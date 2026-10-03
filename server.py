"""Agent Harness: a local web dashboard for the owner and the agents.

- Inbox: agents post questions (yes/no, multiple choice, reviews with media); the owner answers
  with one tap. Agents collect their answers with harness.py.
- Board: what each agent says it is doing, the GPU/build locks, worktrees, processes, disk.

Stdlib only (no pip installs). Run:  python server.py   (binds 0.0.0.0:8765)
Auth: a random token in harness.token; open http://<pc-ip>:8765/?t=<token> once per device and it
is kept in a cookie. Requests from this machine (127.0.0.1) need no token, so harness.py works
without one.
"""

import ctypes
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agents_view
import config
import builds
import quota

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "harness.db")
TOKEN_PATH = os.path.join(HERE, "harness.token")
STATIC = os.path.join(HERE, "static")
UPLOADS = os.path.join(HERE, "uploads")   # screenshots pasted into an agent chat; agents open them with Read
UPLOAD_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
PORT = config.CFG["port"]
REPO = config.CFG["repo"]
LOCK_DIR = config.CFG["lockDir"]
# Only files under these roots can be served as media (read-only).
MEDIA_ROOTS = [os.path.normcase(os.path.abspath(p)) for p in config.CFG["mediaRoots"]]
WATCHED_PROCS = tuple(config.CFG["watchedProcesses"])

_db_lock = threading.Lock()


def token():
    if not os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "w") as f:
            f.write(secrets.token_urlsafe(18))
    with open(TOKEN_PATH) as f:
        return f.read().strip()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,          -- who asks
            kind TEXT NOT NULL,           -- yesno | choice | review | info
            title TEXT NOT NULL,
            body TEXT DEFAULT '',
            options TEXT DEFAULT '[]',    -- JSON list of strings
            media TEXT DEFAULT '[]',      -- JSON list of absolute paths
            recommend TEXT DEFAULT '',    -- the asker's pick, shown as a hint
            priority INTEGER DEFAULT 1,   -- 0 low, 1 normal, 2 urgent
            status TEXT DEFAULT 'open',   -- open | answered | delivered | withdrawn
            answer TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created REAL, answered REAL, delivered REAL
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            t REAL, agent TEXT, kind TEXT,  -- gate | lock | answer | build | note | loop
            text TEXT, status TEXT DEFAULT ''  -- ok | fail | run | ''
        );
        CREATE TABLE IF NOT EXISTS agents(
            name TEXT PRIMARY KEY,
            state TEXT DEFAULT '',        -- working | blocked | idle | waiting
            task TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            updated REAL
        );
        """)


def rows(sql, args=()):
    with _db_lock, db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def log_event(agent, kind, text, status=""):
    execute("INSERT INTO events(t,agent,kind,text,status) VALUES(?,?,?,?,?)",
            (time.time(), agent, kind, text, status))


def lock_watcher():
    """Logs every lock take/release as an event, whether or not a page is open."""
    prev = {}
    while True:
        try:
            cur = {l["lock"]: l["what"] for l in probe_locks()}
            for name in set(prev) | set(cur):
                if prev.get(name) != cur.get(name):
                    if prev.get(name):
                        log_event(prev[name].split("|")[0].strip(), "lock", f"released {name}", "")
                    if cur.get(name):
                        who, _, what = cur[name].partition("|")
                        log_event(who.strip(), "lock", f"took {name}: {what.rsplit('|', 1)[0].strip()}", "run")
            prev = cur
        except Exception:
            pass
        time.sleep(5)


# ---------------------------------------------------------------- system probes (cached)

_cache = {}


_refreshing = set()
_refresh_lock = threading.Lock()


def _refresh(key, fn):
    try:
        val = fn()
    except Exception as e:  # a probe must never take the page down
        val = {"error": str(e)}
    _cache[key] = (time.time(), val)
    with _refresh_lock:
        _refreshing.discard(key)


def cached(key, seconds, fn):
    """Stale-while-revalidate, single-flight: a stale value is returned AT ONCE and refreshed on a
    background thread, never inside the request. The worktree probe runs git in every worktree
    (~11 s with builds running), and computing it inline made /api/state time out for every
    client -- harness.py included -- once a minute."""
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < seconds:
        return hit[1]
    with _refresh_lock:
        start = key not in _refreshing
        if start:
            _refreshing.add(key)
    if hit:
        if start:
            threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
        return hit[1]
    if start:
        _refresh(key, fn)           # first ever call: nothing stale to serve
    hit = _cache.get(key)
    return hit[1] if hit else []


def _run(args, cwd=None, timeout=20):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout


def probe_locks():
    out = []
    if not os.path.isdir(LOCK_DIR):
        return out
    for name in sorted(os.listdir(LOCK_DIR)):
        if not name.endswith(".lock"):
            continue
        p = os.path.join(LOCK_DIR, name)
        try:
            text = open(p, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            continue
        holder = text.split("|")[0].strip() if text else "?"
        out.append({"lock": name[:-5], "holder": holder, "what": text,
                    "minutes": round((time.time() - os.path.getmtime(p)) / 60)})
    return out


def probe_worktrees():
    if not REPO:
        return []
    txt = _run(["git", "-C", REPO, "worktree", "list", "--porcelain"])
    trees, cur = [], {}
    for line in txt.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
            trees.append(cur)
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[5:12]
    for t in trees:
        log = _run(["git", "-C", t["path"], "log", "-1", "--format=%cr|%s"]).strip()
        t["age"], _, t["subject"] = log.partition("|")
        ahead = _run(["git", "-C", t["path"], "rev-list", "--count", "main..HEAD"]).strip()
        t["ahead"] = int(ahead) if ahead.isdigit() else 0
        # --no-optional-locks: a plain `git status` refreshes the index and takes index.lock, which
        # collides with the agents' own commits in that worktree.
        dirty = _run(["git", "--no-optional-locks", "-C", t["path"], "status", "--porcelain", "-uno"]).strip()
        t["dirty"] = len(dirty.splitlines()) if dirty else 0
    return trees


class _MEM(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def probe_system():
    m = _MEM()
    m.dwLength = ctypes.sizeof(_MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    free = ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(config.CFG["disk"], None, None, ctypes.byref(free))
    procs = {}
    for line in _run(["tasklist", "/fo", "csv", "/nh"]).splitlines():
        name = line.split('","')[0].strip('"')
        if name in WATCHED_PROCS:
            procs[name] = procs.get(name, 0) + 1
    gb = 1024 ** 3
    return {"diskFreeGB": round(free.value / gb, 1),
            "ramFreeGB": round(m.ullAvailPhys / gb, 1), "ramTotalGB": round(m.ullTotalPhys / gb, 1),
            "commitFreeGB": round(m.ullAvailPageFile / gb, 1),
            "commitTotalGB": round(m.ullTotalPageFile / gb, 1),
            "procs": procs}


def state():
    return {
        "now": time.time(),
        "config": config.public(),
        "inbox": rows("SELECT * FROM items WHERE status='open' ORDER BY priority DESC, created"),
        "recent": rows("SELECT * FROM items WHERE status IN ('answered','delivered') "
                       "ORDER BY answered DESC LIMIT 30"),
        "agents": rows("SELECT * FROM agents ORDER BY name"),
        "events": rows("SELECT * FROM events ORDER BY t DESC LIMIT 60"),
        "builds": builds.listing(15),
        "quota": quota.snapshot(),
        "agentCards": agents_view.agents(),
        "buildSettings": builds.settings(),
        "locks": cached("locks", 3, probe_locks),
        "worktrees": cached("worktrees", 60, probe_worktrees),
        "system": cached("system", 10, probe_system),
    }


# ---------------------------------------------------------------- HTTP

def _media_allowed(path):
    p = os.path.normcase(os.path.abspath(path))
    return any(p == r or p.startswith(r + os.sep) for r in MEDIA_ROOTS) and os.path.isfile(p)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentHarness/1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- auth: localhost is trusted; anything else needs the token (query once, then cookie)
    def _authed(self, query):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        tok = token()
        if query.get("t", [""])[0] == tok:
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "pxlh" in c and secrets.compare_digest(c["pxlh"].value, tok)

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._send(401, "Open the link with ?t=<token> once on this device.", "text/plain")
        extra = {}
        if q.get("t"):
            extra["Set-Cookie"] = f"pxlh={token()}; Path=/; Max-Age=31536000; SameSite=Strict; HttpOnly"
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"), extra)
        if u.path.startswith("/static/"):
            p = os.path.abspath(os.path.join(STATIC, u.path[len("/static/"):]))
            if p.startswith(STATIC) and os.path.isfile(p):
                return self._file(p, extra)
            return self._send(404, {"error": "not found"})
        if u.path == "/api/state":
            return self._send(200, state(), extra=extra)
        if u.path == "/api/answers":
            agent = q.get("agent", [""])[0]
            items = rows("SELECT * FROM items WHERE agent=? AND status='answered' ORDER BY answered", (agent,))
            if q.get("mark", ["1"])[0] == "1" and items:
                execute(f"UPDATE items SET status='delivered', delivered=? WHERE id IN "
                        f"({','.join(str(i['id']) for i in items)})", (time.time(),))
            return self._send(200, items)
        if u.path == "/api/item":
            r = rows("SELECT * FROM items WHERE id=?", (int(q.get("id", ["0"])[0]),))
            return self._send(200 if r else 404, r[0] if r else {"error": "no such item"})
        if u.path.startswith("/agent/"):
            return self._file(os.path.join(STATIC, "agent.html"), extra)
        if u.path.startswith("/uploads/"):
            p = os.path.abspath(os.path.join(UPLOADS, os.path.basename(u.path)))
            if p.startswith(UPLOADS) and os.path.isfile(p):
                return self._file(p, extra)
            return self._send(404, {"error": "not found"})
        if u.path == "/api/agents":
            return self._send(200, agents_view.agents())
        if u.path.startswith("/api/agent/") and u.path.endswith("/events"):
            name = u.path.split("/")[3]
            code, body = agents_view.host(f"/agent/{name}/events?{u.query}", timeout=40)
            if code == 404 or code == 503:
                code, body = agents_view.terminal_events(name, int(q.get("since", ["0"])[0]))
            return self._send(code, body)
        if u.path == "/api/builds":
            return self._send(200, builds.listing(int(q.get("limit", ["40"])[0])))
        if u.path == "/api/build":
            j = builds.get(int(q.get("id", ["0"])[0]))
            return self._send(200 if j else 404, j or {"error": "no such build"})
        if u.path == "/media":
            return self._media(q.get("p", [""])[0])
        return self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed(parse_qs(u.query)):
            return self._send(401, {"error": "auth"})
        try:
            b = self._json_body()
        except ValueError:
            return self._send(400, {"error": "bad json"})
        now = time.time()
        if u.path == "/api/upload":
            ext = UPLOAD_TYPES.get(b.get("type", ""))
            if not ext:
                return self._send(400, {"error": "png, jpeg, webp or gif only"})
            import base64
            try:
                data = base64.b64decode(b.get("data", ""), validate=True)
            except ValueError:
                return self._send(400, {"error": "bad base64"})
            if not data or len(data) > 25 * 1024 * 1024:
                return self._send(400, {"error": "empty or over 25 MB"})
            os.makedirs(UPLOADS, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3) + "." + ext
            with open(os.path.join(UPLOADS, name), "wb") as f:
                f.write(data)
            return self._send(200, {"path": os.path.join(UPLOADS, name), "url": "/uploads/" + name})
        if u.path == "/api/ask":
            kind = b.get("kind", "yesno")
            opts = b.get("options") or {"yesno": ["Yes", "No"], "review": ["Pass", "Fail"],
                                        "info": ["Seen"]}.get(kind, [])
            if not b.get("agent") or not b.get("title") or not opts:
                return self._send(400, {"error": "agent, title and options (or a kind with defaults) required"})
            iid = execute("INSERT INTO items(agent,kind,title,body,options,media,recommend,priority,created) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (b["agent"], kind, b["title"], b.get("body", ""), json.dumps(opts),
                           json.dumps(b.get("media", [])), b.get("recommend", ""),
                           int(b.get("priority", 1)), now))
            return self._send(200, {"id": iid})
        if u.path == "/api/answer":
            r = rows("SELECT * FROM items WHERE id=?", (int(b.get("id", 0)),))
            if not r:
                return self._send(404, {"error": "no such item"})
            if r[0]["status"] != "open" and not b.get("change"):
                return self._send(409, {"error": "already answered"})
            execute("UPDATE items SET status='answered', answer=?, note=?, answered=?, delivered=NULL WHERE id=?",
                    (b.get("answer", ""), b.get("note", ""), now, r[0]["id"]))
            log_event("Owner", "answer", f"{r[0]['agent']}: {r[0]['title']} -> {b.get('answer', '')}", "ok")
            note = (b.get("note") or "").strip()
            code, _ = agents_view.host(f"/agent/{r[0]['agent']}/send", {"text": f"[Owner via harness inbox, item {r[0]['id']}] "
                                       f"{r[0]['title']} -> {b.get('answer', '')}" + (f". Note: {note}" if note else "")}, timeout=10)
            if code == 200:   # hosted agent: delivered into its chat; terminal agents still collect it
                execute("UPDATE items SET status='delivered', delivered=? WHERE id=?", (time.time(), r[0]["id"]))
            return self._send(200, {"ok": True})
        if u.path == "/api/withdraw":
            execute("UPDATE items SET status='withdrawn' WHERE id=? AND status='open'", (int(b.get("id", 0)),))
            return self._send(200, {"ok": True})
        if u.path == "/api/build":
            try:
                jid = builds.submit(b.get("agent", "?"), b.get("tree", ""), b.get("configs", ["Debug"]),
                                    b.get("target", ""), bool(b.get("configure")), b.get("note", ""),
                                    b.get("buildDir", ""))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"id": jid})
        if u.path == "/api/run":
            try:
                jid = builds.submit_cmd(b.get("agent", "?"), b.get("cwd", ""), b.get("cmd", ""),
                                        bool(b.get("gpu")), b.get("note", ""))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"id": jid})
        if u.path == "/api/agents":
            code, body = agents_view.host("/agents", b, timeout=120)
            if code == 200:
                log_event("Owner", "note", f"new agent {b.get('name')}", "ok")
            return self._send(code, body)
        if u.path.startswith("/api/agent/"):
            parts = u.path.split("/")   # ["", "api", "agent", name, action]
            if len(parts) == 5:
                code, body = agents_view.host(f"/agent/{parts[3]}/{parts[4]}", b, timeout=120)
                return self._send(code, body)
        if u.path == "/api/settings":
            s = builds.save_settings(b)
            log_event("Owner", "note", f"build workers -> {s['buildWorkers']} (2nd needs {s['minCommitGB']} GB commit free)", "ok")
            return self._send(200, s)
        if u.path == "/api/build/cancel":
            builds.cancel(int(b.get("id", 0)))
            return self._send(200, {"ok": True})
        if u.path == "/api/event":
            if not b.get("agent") or not b.get("text"):
                return self._send(400, {"error": "agent and text required"})
            log_event(b["agent"], b.get("kind", "note"), b["text"], b.get("status", ""))
            return self._send(200, {"ok": True})
        if u.path == "/api/status":
            if not b.get("agent"):
                return self._send(400, {"error": "agent required"})
            execute("INSERT INTO agents(name,state,task,detail,updated) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET state=excluded.state, task=excluded.task, "
                    "detail=excluded.detail, updated=excluded.updated",
                    (b["agent"], b.get("state", ""), b.get("task", ""), b.get("detail", ""), now))
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def _file(self, path, extra=None):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype, extra)

    def _media(self, path):
        """Serves an allowed file with HTTP Range support (phone video players require it)."""
        if not _media_allowed(path):
            return self._send(404, {"error": "not an allowed media file"})
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        code = 200
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the player seeks by dropping connections; that is normal


def main():
    os.makedirs(STATIC, exist_ok=True)
    init_db()
    tok = token()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=lock_watcher, daemon=True).start()
    builds.init_db(log_event)   # jobs and the quota probe run in host.py
    srv.daemon_threads = True
    print(f"Agent Harness on http://localhost:{PORT}/  (phone: http://<this-pc-ip>:{PORT}/?t={tok})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
