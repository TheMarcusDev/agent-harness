# Agent Harness

A local, self-hosted control room for running **several Claude Code agents in parallel on one
Windows machine**, with you as the person they report to.

- **Inbox**: agents ask you decisions (yes/no, multiple choice, pass/fail reviews with inline
  videos and images) and you answer with one tap, from the PC or your phone. Answers are delivered
  back to the agent automatically.
- **Agent chat windows**: every agent is a real Claude Code session hosted in the background (no
  terminals). Each opens in its own minimal browser window: streaming tool calls, permission cards
  (Allow / Always / Deny), **Stop**, **Stop & send** (interrupt and answer you now), screenshot
  paste/drop/attach, restart, full history kept across restarts.
- **Dashboard**: an orbit view of the agents and the locks they hold, a board, a timeline, the
  job queue, machine meters (CPU, RAM, commit, disk), Claude quota (5-hour and weekly), locks,
  git worktrees.
- **Job queue**: agents submit builds and GPU work to the harness instead of running them in
  their own shells. Jobs survive the agent's process being killed, builds are throttled by free
  memory, and GPU jobs wait their turn in a FIFO **gpu.lock** so two captures never overlap.

Python standard library plus `claude-agent-sdk`. Windows only (it uses PowerShell, Git Bash and a
few Win32 calls).

## Requirements

- Windows 10/11, Python 3.11+ (`python` and `pythonw` on PATH)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in (`claude` on PATH)
- Git for Windows (its `bash.exe` runs queued commands)
- `pip install -r requirements.txt`

## Quick start

```powershell
git clone <this repo> C:\AgentHarness
cd C:\AgentHarness
pip install -r requirements.txt
copy harness.config.example.json harness.config.json   # then edit it, see below
start-harness.cmd                                      # starts 3 background processes, opens the dashboard
```

The dashboard is <http://localhost:8765/>. Click **+ add agent** to create one (name, colour,
working directory); its chat window opens. Add the agent rules below to your project's `CLAUDE.md`
so agents know the harness exists.

## The three processes

| Process | Port | Job | Safe to restart? |
| --- | --- | --- | --- |
| `server.py` | 8765 (all interfaces, token-protected) | dashboard + API | any time |
| `host.py` | 8766 (guard only) | build queue, job queue, quota probe | only when no job is RUNNING |
| `agenthost.py` | 8767 (localhost) | hosts the agent sessions | only when every agent is idle |

`start-harness.cmd` launches `host.py` and `agenthost.py` under `python.exe` with a **hidden
console**, never `pythonw`: they spawn `claude.exe` children, and under `pythonw` each child pops
its own blank console window (closing one kills that agent). Keep it that way.

Local state (all gitignored): `harness.db` (inbox, events, jobs), `agents.json` (agent registry:
session ids, colours, cwd), `harness.json` (queue settings), `harness.token`, `quota.json`,
`build-logs/`, `uploads/` (pasted screenshots).

## Configuration: `harness.config.json`

Every key is optional; defaults live in `config.py`.

| Key | Meaning |
| --- | --- |
| `projectName`, `subtitle` | dashboard title |
| `ownerName` | the inbox heading, "Needs <ownerName>" |
| `coordinator` | the agent drawn at the centre of the orbit (a lead/reviewer agent) |
| `defaultCwd` | working directory offered when adding an agent |
| `repo` | git repo whose worktrees the dashboard lists (`""` hides the panel) |
| `port` | dashboard port (env `HARNESS_PORT` overrides) |
| `lockDir` | folder holding `gpu.lock` and the `build.N.lock` slots |
| `gpuLockScript` | the FIFO lock script; default is the bundled `tools/gpu-lock.ps1` |
| `bash` | Git Bash used to run queued commands |
| `disk` | drive whose free space the Machine panel shows (default: the harness's own drive) |
| `mediaRoots` | folders whose files may be shown in the inbox (read-only); nothing else is served |
| `watchedProcesses` | process names shown under Machine while running (your editor, Blender...) |
| `agentColors`, `agentShort` | colour and short label per agent (the registry colour wins) |

Queue settings live in `harness.json` and are changed from the dashboard (Queue tab):
`buildWorkers` (1 or 2), `minCommitGB` (a second build starts only if this much commit charge is
free), `buildParallel` (`--parallel` passed to the build), `buildLockSlots`.

## Agent side: `harness.py`

Agents talk to the harness through `harness.py` (prints JSON; exit 1 if the server is down):

```bash
# ask the owner (kinds: yesno, choice, review, info)
python harness.py ask --agent Artist --kind review --title "Ship walk_cycle_v3?" \
       --media C:/renders/walk.mp4 --body "..." --recommend Pass
python harness.py ask --agent Designer --kind choice --title "Shop location?" --options "East" "West" --recommend East
python harness.py answers --agent Artist          # collect answers (hosted agents also get them pushed)
python harness.py status --agent Artist --state working --task "rig export" --detail "18 clips"
python harness.py event --agent Reviewer --kind gate --status ok --text "branch landed abc1234"

# builds (CMake: configure + build per config) and arbitrary commands
python harness.py build --agent Builder --tree C:/src/myproject --config Debug Release --wait
python harness.py run --agent Renderer --cwd C:/src/myproject --gpu --note "captures" \
       --cmd "python tests/run_captures.py" --wait
python harness.py build-status --id 7 --wait     # re-attach; never resubmit a job that is still running
python harness.py builds                         # the queue
python harness.py build-cancel --id 7
```

Media for `--media` must live under one of `mediaRoots`.

### Rules to put in your project's CLAUDE.md

```markdown
## Agent harness
- Ask the owner through `python <harness>/harness.py ask ...`, not in chat, when a decision is his.
- Builds and every GPU run (captures, editor launches, tests that start the app) go through
  `harness.py build` / `harness.py run --gpu`, never your own shell: harness jobs survive your
  process being killed, and the GPU queue is first-come-first-served.
- If your wait times out, re-attach with `harness.py build-status --id N --wait`; do not resubmit.
- Report status with `harness.py status` when you start and finish a task.
```

## Phone and remote access

Requests from this PC need no token. Anything else needs the token in `harness.token` (created on
first start): open `http://<pc>:8765/?t=<token>` once per device and a cookie keeps it.

The server speaks **plain HTTP** and the chat windows drive agents that can run commands on your
machine, so **do not port-forward it or put it behind a public tunnel**. For access away from home
use [Tailscale](https://tailscale.com): install it on the PC and the phone, then browse to
`http://<tailscale-ip>:8765/?t=<token>`. If the page does not load, allow the port on the Tailscale
interface only (admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "Agent Harness (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort 8765 -InterfaceAlias Tailscale -RemoteAddress 100.64.0.0/10 -Action Allow
```

A tunnel that forwards from localhost (e.g. `tailscale serve`, cloudflared) would bypass the token,
because localhost is trusted. Browse to the Tailscale IP directly instead.

## Moving existing terminal sessions in

A Claude Code session started with `claude --name <Name>` can be moved into the harness with its
history: `python tools/migrate_terminal_agent.py <Name> "#5aa2ff"`. It ends the terminal session
(do it between turns) and resumes the same session id in the agent host.

## Setting it up with Claude (instructions for your Claude Code)

If you are a Claude Code agent asked to set this harness up for a user's project, do this, in order,
and ask the user only where marked:

1. **Check prerequisites**: `python --version` (3.11+), `claude --version`, Git Bash at the path in
   `config.py` (`bash`), then `pip install -r requirements.txt`.
2. **Write `harness.config.json`** from `harness.config.example.json`. Ask the user for: the
   project name, their name (for the inbox), and the agent they want at the centre (default
   `Reviewer`). Derive the rest yourself: `defaultCwd` and `repo` = the project's root (`repo` only
   if it is a git repo), `mediaRoots` = folders where the project writes renders, screenshots or
   test output, `watchedProcesses` = the project's own executables plus tools it uses (Blender,
   Unreal, MSBuild...). Keep `lockDir` and `gpuLockScript` at their defaults unless the project
   already has its own lock script; if it does, point `gpuLockScript` at it and `lockDir` at the
   folder that script uses, so both queue in ONE place.
3. **Adapt the queue to the project's build.** `builds.py` assumes CMake (`cmake -S . -B <dir>`,
   `cmake --build <dir> --config <cfg>`). For another build system, change the `steps` list in
   `builds.py`'s `_run_build`, or have agents use `harness.py run` with the build command instead.
4. **Start it** with `start-harness.cmd` and confirm `http://localhost:8765/api/state` answers.
5. **Create the agents** the user wants (dashboard **+ add agent**, or
   `POST /api/agents {"name", "color", "cwd"}`), one per role. Never more than the user asks for.
6. **Add the CLAUDE.md rules** above to the project, adjusting paths.
7. **Remote access** only if the user asks: Tailscale as described, never a public tunnel, and
   never print or commit `harness.token`.
8. Restart rules: `server.py` any time; `host.py` only with no job running; `agenthost.py` only
   with every agent idle (it hosts them, including possibly yourself; restart it from a harness job
   such as `harness.py run`, never from a hosted agent's own shell).

## License

MIT, see `LICENSE`.
