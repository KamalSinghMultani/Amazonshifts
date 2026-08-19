@echo off
REM Run the optimized schedule-aware watcher, restarting it if it ever exits.
REM
REM Detection stays in the main process; session health/re-login runs in a
REM separate helper process so OTP/challenge work cannot pause the 2s detector.
REM
REM Usage:  run_watcher.bat            (Canada, live)
REM         run_watcher.bat config.us.yaml

setlocal
cd /d "%~dp0"

set CONFIG=%~1
if "%CONFIG%"=="" set CONFIG=config.yaml

:loop
echo [%date% %time%] starting optimized watcher with %CONFIG%
".venv\Scripts\python.exe" watcher_v3.py --config "%CONFIG%"
echo [%date% %time%] watcher exited with code %errorlevel% - restarting in 30s
REM 30s, not instantly: if it is failing because Amazon is blocking us, a
REM tight restart loop makes that worse rather than better.
timeout /t 30 /nobreak >nul
goto loop
