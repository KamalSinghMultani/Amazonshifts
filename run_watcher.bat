@echo off
REM Run the final live watcher, restarting it if it ever exits.
REM
REM Detection stays in the main process. Session health proof and recovery run
REM in isolated helpers so authentication work cannot pause detection.
REM v6 separates prove-only health checks from login recovery and refuses to
REM attempt a hold unless the protected application session is recently verified.
REM
REM Usage:  run_watcher.bat            (Canada, live)
REM         run_watcher.bat config.us.yaml

setlocal
cd /d "%~dp0"

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.yaml

:loop
echo [%date% %time%] starting optimized watcher v6 with %CONFIG%
".venv\Scripts\python.exe" watcher_v6.py --config "%CONFIG%"
echo [%date% %time%] watcher exited with code %errorlevel% - restarting in 30s
REM 30s, not instantly: if the site is rejecting requests, a tight restart loop
REM makes the outage worse rather than helping recovery.
timeout /t 30 /nobreak >nul
goto loop
