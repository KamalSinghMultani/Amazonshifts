@echo off
REM Run the schedule-aware watcher, restarting it if it ever exits.
REM
REM The watcher already survives ordinary failures on its own: a bad poll is
REM caught, and repeated failures trip a circuit breaker that backs off. This
REM loop is for the things it cannot catch — the browser being killed, a
REM Playwright driver crash, a Windows update closing it.
REM
REM Usage:  run_watcher.bat            (Canada, live)
REM         run_watcher.bat config.us.yaml

setlocal
cd /d "%~dp0"

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.yaml

:loop
echo [%date% %time%] starting schedule-aware watcher with %CONFIG%
".venv\Scripts\python.exe" watcher_v2.py --config "%CONFIG%"
echo [%date% %time%] watcher exited with code %errorlevel% - restarting in 30s
REM 30s, not instantly: if it is failing because Amazon is blocking us, a
REM tight restart loop makes that worse rather than better.
timeout /t 30 /nobreak >nul
goto loop
