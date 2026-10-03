"""Claude plan quota for the dashboard.

The plan's usage windows (5-hour, weekly) are not stored anywhere on disk; they arrive as a
RateLimitEvent on every Claude Agent SDK request (raw["unifiedWindows"]). Until the agents run
inside the harness -- whose own events will then feed `update()` for free -- a tiny Haiku ping
every POLL_SECONDS keeps the numbers fresh. One ping measured $0.0046.
"""

import asyncio
import json
import os
import threading
import time

QUOTA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quota.json")

POLL_SECONDS = 15 * 60
_state = {"windows": {}, "status": "", "updated": 0, "error": ""}
_lock = threading.Lock()


def update(raw):
    """Feed a RateLimitInfo.raw dict (from a probe or from any hosted agent's stream)."""
    if not raw:
        return
    with _lock:
        wins = raw.get("unifiedWindows") or {}
        if wins:
            _state["windows"] = {k: {"utilization": v.get("utilization"), "resetsAt": v.get("resetsAt")}
                                 for k, v in wins.items()}
        _state["status"] = raw.get("status", "")
        _state["updated"] = time.time()
        _state["error"] = ""
        _save()


def _save():
    try:
        with open(QUOTA_PATH, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except OSError:
        pass


def snapshot():
    """Read by the web server: the host process owns the probe and writes quota.json."""
    try:
        with open(QUOTA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        with _lock:
            return dict(_state)


async def _probe():
    from claude_agent_sdk import ClaudeAgentOptions, RateLimitEvent, query
    opts = ClaudeAgentOptions(model="claude-haiku-4-5-20251001", max_turns=1, setting_sources=[], tools=[])
    async for m in query(prompt="Reply with the single word: ok", options=opts):
        if isinstance(m, RateLimitEvent):
            update(m.rate_limit_info.raw)


def _loop():
    while True:
        try:
            asyncio.run(_probe())
        except Exception as e:  # never take the server down over a quota ping
            with _lock:
                _state["error"] = str(e)[:200]
            _save()
        # Skip the ping when a hosted agent reported recently (its events are free).
        time.sleep(POLL_SECONDS)
        while time.time() - _state["updated"] < POLL_SECONDS - 30:
            time.sleep(60)


def start():
    threading.Thread(target=_loop, daemon=True).start()
