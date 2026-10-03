"""Build queue worker for the Agent Harness.

Agents submit builds; the harness runs them one (or two) at a time, in arrival order. Jobs run as
children of host.py -- a long-lived process separate from the web server, so restarting the
dashboard never kills a job -- not of an agent's shell, so Claude Code's low-memory reaper
-- which kills background commands of idle agent sessions -- cannot kill it: an agent's wait can be
reaped and the build still finishes, with its log and exit code kept here.

Concurrency is decided by MEASUREMENT, not a fixed rule: a job starts only while fewer than
`workers` are running AND free commit (the Windows commit limit minus the committed bytes, which
is what an out-of-memory build actually runs out of) is at least `minCommitGB`. A second worker is
therefore used only when the machine has room for it.

It also takes a build-lock slot (a build.N.lock file in the configured lockDir), so a project
script that takes the same slots by hand is never overrun, and waits for a harness build the same way.
"""

import ctypes
import json
import os
import sqlite3
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "harness.db")
LOG_DIR = os.path.join(HERE, "build-logs")
SETTINGS_PATH = os.path.join(HERE, "harness.json")
import config
LOCK_DIR = config.CFG["lockDir"]
COORDINATOR = config.CFG["coordinator"]
DEFAULTS = {"buildWorkers": 1, "minCommitGB": 12, "buildParallel": 3, "buildLockSlots": 2}

_lock = threading.Lock()
_procs = {}            # job id -> Popen of the running build
_on_event = None       # callback(agent, kind, text, status) for the activity log


def settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(update):
    s = settings()
    if 'buildWorkers' in update:
        s['buildWorkers'] = 2 if int(update['buildWorkers']) >= 2 else 1
    if 'minCommitGB' in update:
        s['minCommitGB'] = max(4, min(40, int(update['minCommitGB'])))
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(s, f)
    return s


def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db(on_event=None):
    global _on_event
    if on_event:
        _on_event = on_event
    os.makedirs(LOG_DIR, exist_ok=True)
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS builds(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            tree TEXT NOT NULL,             -- worktree root, e.g. C:\\src\\myproject
            configs TEXT NOT NULL,          -- JSON list, e.g. ["Debug","Release"]
            target TEXT DEFAULT '',         -- optional --target
            configure INTEGER DEFAULT 0,    -- run cmake -S . -B build first
            note TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',   -- queued | running | ok | fail | cancelled
            exit_code INTEGER,
            step TEXT DEFAULT '',           -- which config is building now
            slot TEXT DEFAULT '',
            log TEXT DEFAULT '',
            created REAL, started REAL, finished REAL
        );
        """)
        cols = {r[1] for r in c.execute('PRAGMA table_info(builds)')}
        for col, decl in (('build_dir', "TEXT DEFAULT ''"), ('kind', "TEXT DEFAULT 'build'"), ('cmd', "TEXT DEFAULT ''"),
                          ('cwd', "TEXT DEFAULT ''"), ('gpu', 'INTEGER DEFAULT 0')):
            if col not in cols:
                c.execute(f'ALTER TABLE builds ADD COLUMN {col} {decl}')


def start_host(on_event):
    """Host process only. Recovers what the previous host left: a build or a job still WAITING for the
    GPU never started, so it is simply queued again (a rebuild is idempotent too); a command job that
    had started is marked failed, since re-running an arbitrary command is not known to be safe."""
    global _on_event
    _on_event = on_event
    init_db()
    _kill_orphan_acquires()
    now = time.time()
    _exec("UPDATE builds SET status='queued', step='', note=note||' [requeued: host restarted]' "
          "WHERE status IN ('running','waiting') AND kind='build'")
    _exec("UPDATE builds SET status='queued', note=note||' [requeued: host restarted while waiting]' "
          "WHERE status='waiting' AND kind='cmd'")
    _exec("UPDATE builds SET status='fail', exit_code=-2, finished=?, note=note||' [host restarted mid-run]' "
          "WHERE status='running' AND kind='cmd'", (now,))
    for fn in (_worker, _cmd_dispatcher, _cancel_watcher):
        threading.Thread(target=fn, daemon=True).start()


def _kill_orphan_acquires():
    """A gpu.lock acquire started by a PREVIOUS host survives it (only python.exe is killed) and goes on
    waiting in the FIFO queue. When its turn comes it takes the lock as 'H<job>-<agent>' and exits,
    with nothing left to run the job or release the lock -- and the requeued copy of that job then waits
    forever behind its own ghost. That stalled the whole queue for 36 min once. Every harness-named
    acquire alive at host start is such an orphan, so end them all before requeueing."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'powershell.exe' -and "
          "$_.CommandLine -match 'gpu-lock\\.ps1' -and $_.CommandLine -match ' acquire H\\d+-' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _rows(sql, args=()):
    with _lock, _db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with _lock, _db() as c:
        return c.execute(sql, args).lastrowid


def find_build_dir(tree, explicit=""):
    """Each worktree names its build dir differently (build, build-gameplay, ...): use the explicit one,
    else the one holding a CMakeCache.txt, preferring build-<worktree suffix>."""
    if explicit:
        return explicit
    suffix = os.path.basename(tree).split('-', 1)[-1]
    for name in (f'build-{suffix}', 'build') + tuple(sorted(d for d in os.listdir(tree) if d.startswith('build-'))):
        if os.path.isfile(os.path.join(tree, name, 'CMakeCache.txt')):
            return name
    return 'build'


def submit(agent, tree, configs, target="", configure=False, note="", build_dir=""):
    tree = os.path.abspath(tree)
    if not os.path.isfile(os.path.join(tree, "CMakeLists.txt")):
        raise ValueError(f"{tree} has no CMakeLists.txt")
    build_dir = find_build_dir(tree, build_dir)
    if not configure and not os.path.isfile(os.path.join(tree, build_dir, 'CMakeCache.txt')):
        raise ValueError(f"{tree}\\{build_dir} is not a configured build dir (pass --build-dir or --configure)")
    configs = [c for c in configs if c in ("Debug", "Release")] or ["Debug"]
    jid = _exec("INSERT INTO builds(agent,tree,configs,target,configure,note,build_dir,created) VALUES(?,?,?,?,?,?,?,?)",
                (agent, tree, json.dumps(configs), target, 1 if configure else 0, note, build_dir, time.time()))
    _event(agent, f"queued build #{jid}: {os.path.basename(tree)} {'+'.join(configs)}{' --target ' + target if target else ''}", "")
    return jid


BASH = config.CFG["bash"]
GPU_LOCK = config.CFG["gpuLockScript"]
# The bundled tools/gpu-lock.ps1 reads its lock folder from here, so the script and the harness agree.
os.environ["HARNESS_LOCK_DIR"] = LOCK_DIR


def submit_cmd(agent, cwd, cmd, gpu=False, note=""):
    """Runs a bash command as a harness child (reaper-proof). gpu=True waits its turn in the FIFO
    gpu.lock queue first, exactly like an agent's own acquire, and releases after."""
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise ValueError(f"{cwd} is not a directory")
    if not cmd.strip():
        raise ValueError("empty command")
    jid = _exec("INSERT INTO builds(agent,tree,configs,kind,cmd,cwd,gpu,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (agent, cwd, "[]", "cmd", cmd, cwd, 1 if gpu else 0, note, time.time()))
    _event(agent, f"queued job #{jid}{' (gpu)' if gpu else ''}: {note or cmd[:60]}", "")
    return jid


def _cmd_dispatcher():
    while True:
        try:
            for job in _rows("SELECT id FROM builds WHERE status='queued' AND kind='cmd' ORDER BY id"):
                _exec("UPDATE builds SET status='waiting' WHERE id=?", (job['id'],))
                threading.Thread(target=_run_cmd, args=(job['id'],), daemon=True).start()
        except Exception:
            pass
        time.sleep(2)


def _free_dead_harness_lock():
    """A gpu.lock held as 'H<job>-<agent>' belongs to a harness job. If that job is no longer
    running or waiting in THIS host (killed, host restarted), nothing will ever release it, and
    the whole FIFO queue stalls behind it -- which happened at the first host restart. Free it."""
    path = os.path.join(LOCK_DIR, 'gpu.lock')
    try:
        holder = open(path, encoding='utf-8', errors='replace').read().split('|')[0].strip()
    except OSError:
        return
    if not holder.startswith('H') or '-' not in holder or not holder[1:holder.index('-')].isdigit():
        return
    jid = int(holder[1:holder.index('-')])
    r = _rows('SELECT status FROM builds WHERE id=?', (jid,))
    st = r[0]['status'] if r else None
    with _lock:
        alive = jid in _procs
    if st == 'running' and alive:
        return   # the job is running its command and holds the lock legitimately
    if st in ('running', 'waiting') and time.time() - os.path.getmtime(path) < 30:
        return   # just acquired; the job thread is about to flip to running
    # 'waiting' for longer than that means the lock was taken by a ghost acquire, not by this job's
    # own waiter (which is still in the queue behind it): free it, or the job waits on itself forever.
    try:
        os.remove(path)
        _event(COORDINATOR, f'freed gpu.lock left by dead harness job #{jid}', 'fail')
    except OSError:
        pass


def _cancel_watcher():
    """The web server only FLAGS a cancel in the database; the host owns the processes and kills them."""
    while True:
        try:
            with _lock:
                live = dict(_procs)
            for jid, p in live.items():
                if _status(jid) == 'cancelled':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True)
            _free_dead_harness_lock()
        except Exception:
            pass
        time.sleep(2)


def _popen(jid, args, **kw):
    # A host started with a hidden console and redirected stdout/stderr has an INVALID stdin handle;
    # a job inheriting it breaks any Python child that calls subprocess without stdin= (WinError 6 in
    # GetStdHandle -- run.py did). Give every job a real, empty stdin.
    kw.setdefault("stdin", subprocess.DEVNULL)
    p = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kw)
    with _lock:
        _procs[jid] = p
    rc = p.wait()
    with _lock:
        _procs.pop(jid, None)
    return rc


def _status(jid):
    return _rows("SELECT status FROM builds WHERE id=?", (jid,))[0]["status"]


def _run_cmd(jid):
    job = _rows("SELECT * FROM builds WHERE id=?", (jid,))[0]
    log = os.path.join(LOG_DIR, f"job-{jid}.log")
    holder = f"H{jid}-{job['agent']}"
    _exec("UPDATE builds SET status='waiting', log=? WHERE id=?", (log, jid))
    rc = -1
    acquired = False
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        try:
            if job["gpu"]:
                out.write(f"===== gpu.lock acquire as {holder}\n")
                out.flush()
                what = (job["note"] or job["cmd"])[:80] + f" (harness #{jid})"
                _popen(jid, ["powershell", "-ExecutionPolicy", "Bypass", "-File", GPU_LOCK, "acquire", holder, what],
                       stdout=out, stderr=subprocess.STDOUT)
                acquired = True
                if _status(jid) == "cancelled":
                    return
            _exec("UPDATE builds SET status='running', started=?, step='run' WHERE id=?", (time.time(), jid))
            _event(job["agent"], f"started job #{jid}: {job['note'] or job['cmd'][:60]}", "run")
            out.write(f"===== run in {job['cwd']}: {job['cmd']}\n")
            out.flush()
            rc = _popen(jid, [BASH, "-lc", job["cmd"]], cwd=job["cwd"], stdout=out, stderr=subprocess.STDOUT,
                        env=dict(os.environ, MSBUILDDISABLENODEREUSE="1"))
            out.write(f"===== exit {rc}\n")
        finally:
            if acquired:
                subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", GPU_LOCK, "release", holder],
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if _status(jid) == "cancelled":
        _event(job["agent"], f"job #{jid} cancelled", "fail")
        return
    final = "ok" if rc == 0 else "fail"
    _exec("UPDATE builds SET status=?, exit_code=?, finished=? WHERE id=?", (final, rc, time.time(), jid))
    _event(job["agent"], f"job #{jid} {final} (exit {rc}): {job['note'] or job['cmd'][:60]}", final)


def get(jid):
    r = _rows("SELECT * FROM builds WHERE id=?", (jid,))
    if not r:
        return None
    job = r[0]
    job["position"] = _position(job)
    job["logTail"] = _tail(job["log"])
    return job


def listing(limit=40):
    jobs = _rows("SELECT * FROM builds ORDER BY id DESC LIMIT ?", (limit,))
    for j in jobs:
        j["position"] = _position(j)
    return jobs


def cancel(jid):
    """Flags the job; the host's cancel watcher kills its process tree within ~2 s."""
    _exec("UPDATE builds SET status='cancelled', finished=? WHERE id=? AND status IN ('queued','running','waiting')",
          (time.time(), jid))


def _position(job):
    if job["status"] != "queued":
        return 0
    return len(_rows("SELECT id FROM builds WHERE status='queued' AND kind='build' AND id<=?", (job["id"],)))


def _tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def _event(agent, text, status):
    if _on_event:
        try:
            _on_event(agent, "build", text, status)
        except Exception:
            pass


class _MEM(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def commit_free_gb():
    m = _MEM()
    m.dwLength = ctypes.sizeof(_MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPageFile / 1024 ** 3


def _take_slot(job):
    """Atomically create a free build.N.lock, exactly as build-lock.ps1 would. Returns its name."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    for n in range(1, settings()["buildLockSlots"] + 1):
        path = os.path.join(LOCK_DIR, f"build.{n}.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.write(fd, f"Harness:{job['agent']} | build #{job['id']} {os.path.basename(job['tree'])} | "
                     f"{time.strftime('%Y-%m-%dT%H:%M:%S')}".encode())
        os.close(fd)
        return path
    return ""


def _release_slot(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _run(job, slot):
    jid = job["id"]
    log = os.path.join(LOG_DIR, f"build-{jid}.log")
    _exec("UPDATE builds SET status='running', started=?, slot=?, log=? WHERE id=?",
          (time.time(), os.path.basename(slot), log, jid))
    _event(job["agent"], f"started build #{jid}: {os.path.basename(job['tree'])}", "run")
    env = dict(os.environ, MSBUILDDISABLENODEREUSE="1")
    par = str(settings()["buildParallel"])
    steps = []
    if job["configure"]:
        steps.append(("configure", ["cmake", "-S", ".", "-B", job["build_dir"] or "build"]))
    for cfg in json.loads(job["configs"]):
        cmd = ["cmake", "--build", job["build_dir"] or "build", "--config", cfg, "--parallel", par]
        if job["target"]:
            cmd += ["--target", job["target"]]
        steps.append((cfg, cmd))
    rc = 0
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        for name, cmd in steps:
            _exec("UPDATE builds SET step=? WHERE id=?", (name, jid))
            out.write(f"\n===== {name}: {' '.join(cmd)}\n")
            out.flush()
            p = subprocess.Popen(cmd, cwd=job["tree"], env=env, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with _lock:
                _procs[jid] = p
            rc = p.wait()
            with _lock:
                _procs.pop(jid, None)
            out.write(f"===== {name}: exit {rc}\n")
            if rc != 0:
                break
    _release_slot(slot)
    status = _rows("SELECT status FROM builds WHERE id=?", (jid,))[0]["status"]
    if status == "cancelled":
        _event(job["agent"], f"build #{jid} cancelled", "fail")
        return
    final = "ok" if rc == 0 else "fail"
    _exec("UPDATE builds SET status=?, exit_code=?, finished=? WHERE id=?", (final, rc, time.time(), jid))
    dur = time.time() - _rows("SELECT started FROM builds WHERE id=?", (jid,))[0]["started"]
    _event(job["agent"], f"build #{jid} {final} (exit {rc}, {dur / 60:.1f} min): {os.path.basename(job['tree'])}", final)


def _worker():
    while True:
        try:
            s = settings()
            busy = len(_rows("SELECT id FROM builds WHERE status='running' AND kind='build'"))
            nxt = _rows("SELECT * FROM builds WHERE status='queued' AND kind='build' ORDER BY id LIMIT 1")
            # Second (or later) concurrent build only with measured headroom; the first always may start.
            room = busy == 0 or commit_free_gb() >= s["minCommitGB"]
            if nxt and busy < s["buildWorkers"] and room:
                slot = _take_slot(nxt[0])
                if slot:
                    threading.Thread(target=_run, args=(nxt[0], slot), daemon=True).start()
                    time.sleep(20)   # let the new build's memory show up before judging room for another
                    continue
        except Exception:
            pass
        time.sleep(3)
