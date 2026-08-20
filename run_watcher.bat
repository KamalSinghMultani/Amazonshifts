@echo off
REM Run the final pre-live watcher, restarting it if it ever exits.
REM
REM Detection stays in the main process; verified session health/re-login runs
REM in a helper process so authentication work cannot pause detection.
REM v5 adds failure screenshots, event-driven direct holding, latency records,
REM and cleanup of an in-flight session helper on shutdown.
REM
REM Usage:  run_watcher.bat            (Canada, live)
REM         run_watcher.bat config.us.yaml

setlocal
cd /d "%~dp0"

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.yaml

:loop
echo [%date% %time%] starting optimized watcher v5 with %CONFIG%
".venv\Scripts\python.exe" watcher_v5.py --config "%CONFIG%"
echo [%date% %time%] watcher exited with code %errorlevel% - restarting in 30s
REM 30s, not instantly: if the site is rejecting requests, a tight restart loop
REM makes the outage worse rather than helping recovery.
timeout /t 30 /nobreak >nul
goto loop
