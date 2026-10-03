@echo off
rem Starts the Agent Harness in the background and opens the dashboard:
rem   agenthost.py  hosts the agent sessions (chat windows)      127.0.0.1:8767
rem   host.py       runs builds, captures, tests, quota probe    127.0.0.1:8766 (guard)
rem   server.py     the dashboard, restartable at any time       0.0.0.0:8765
rem Safe to run when already running: a second copy of each fails to bind its port and exits.
rem
rem agenthost runs under python.exe with a HIDDEN console, never pythonw: each agent is a claude.exe
rem console child, and under pythonw (no console) every child pops its own blank console window --
rem closing one kills that agent -- and the session only registers with its peers after its first turn.
cd /d "%~dp0"
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath python -ArgumentList 'agenthost.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardError '%~dp0agenthost.err.log' -RedirectStandardOutput '%~dp0agenthost.out.log'"
rem host.py too: its quota probe spawns claude.exe, which under pythonw flashes a blank console every 15 min.
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath python -ArgumentList 'host.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardError '%~dp0host.err.log' -RedirectStandardOutput '%~dp0host.out.log'"
start "" pythonw server.py
timeout /t 2 /nobreak >nul
start "" http://localhost:8765/
