"""Harness configuration: built-in defaults, overridden by harness.config.json beside this file.

harness.config.json is LOCAL (gitignored): it names your machine's paths and your project.
Copy harness.config.example.json to start one. Every key is optional.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))

DEFAULTS = {
    "projectName": "Agent Harness",          # big title on the dashboard
    "subtitle": "Command Centre",            # line under it
    "ownerName": "you",                      # "Needs <ownerName>" inbox heading
    "coordinator": "Reviewer",               # the agent drawn at the centre of the orbit
    "defaultCwd": "",                        # working dir offered when adding an agent ("" = home)
    "repo": "",                              # git repo whose worktrees the dashboard lists ("" = none)
    "port": 8765,                            # dashboard port (agent host 8767, job host 8766)
    "lockDir": os.path.join(_LOCALAPPDATA, "AgentHarness"),   # gpu.lock + build.N.lock files
    "gpuLockScript": os.path.join(HERE, "tools", "gpu-lock.ps1"),
    "bash": r"C:\Program Files\Git\bin\bash.exe",
    "disk": os.path.splitdrive(HERE)[0] + "\\",   # drive whose free space the Machine panel shows
    "mediaRoots": [],                        # folders whose files the inbox may show (read-only)
    "watchedProcesses": ["MSBuild.exe", "blender.exe"],   # shown under Machine when running
    "agentColors": {},                       # name -> "#rrggbb" (agents.json colours win)
    "agentShort": {},                        # name -> short label for the orbit legend
}


def _load():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "harness.config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["defaultCwd"] = cfg["defaultCwd"] or os.path.expanduser("~")
    cfg["port"] = int(os.environ.get("HARNESS_PORT", cfg["port"]))
    return cfg


CFG = _load()


def public():
    """The subset the dashboard needs: names and labels, never paths beyond the default cwd."""
    return {k: CFG[k] for k in ("projectName", "subtitle", "ownerName", "coordinator", "defaultCwd",
                                "agentColors", "agentShort")}
