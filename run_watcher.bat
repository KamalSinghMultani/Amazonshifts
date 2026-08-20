@echo off
REM Run the optimized schedule-aware watcher, restarting it if it ever exits.
REM
REM Detection stays in the main process; session health/re-login runs in a
REM separate helper process so OTP/challenge work cannot pause the detector.
REM v4 also starts that session bootstrap immediately on a live startup instead
REM of waiting for the first periodic health-check window.
REM
REM Usage:  run_watcher.bat            (Canada, live)
REM         run_watcher.bat config.us.yaml

setlocal
cd /d "%~dp0"

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.yaml

:loop
echo [%date% %time%] starting optimized watcher v4 with %CONFIG%
".venv\Scripts\python.exe" watcher_v4.py --config "%CONFIG%"
echo [%date% %time%] watcher exited with code %errorlevel% - restarting in 30s
REM 30s, not instantly: if the site is rejecting requests, a tight restart loop
REM makes the outage worse rather than helping recovery.
timeout /t 30 /nobreak >nul
goto loop
