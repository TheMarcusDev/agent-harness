"""Agent Harness job host: runs builds, captures, tests and the quota probe.

Long-lived and SEPARATE from the web server (server.py), so the dashboard can be restarted or
updated at any time without killing a job. The two share only harness.db (jobs, events) and
quota.json. A second host refuses to start (it binds 127.0.0.1:8766 as a singleton guard).

Restarting THIS process does stop running jobs: builds and still-waiting jobs are re-queued
automatically on the next start; a command job that had already started is marked failed.
Run:  pythonw host.py   (start-harness.cmd starts it with the server)
"""

import os
import socket
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import builds  # noqa: E402
import quota   # noqa: E402

DB_PATH = os.path.join(HERE, "harness.db")


def log_event(agent, kind, text, status=""):
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        c.execute("INSERT INTO events(t,agent,kind,text,status) VALUES(?,?,?,?,?)", (time.time(), agent, kind, text, status))


def main():
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", 8766))
    except OSError:
        print("another harness host is already running")
        return
    guard.listen(1)
    builds.start_host(log_event)
    quota.start()
    log_event(builds.COORDINATOR, "note", "harness host started", "ok")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
