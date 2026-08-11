@echo off
REM ---------------------------------------------------------------------------
REM Windows Task Scheduler entry point for the 6 AM morning report.
REM
REM Calls the venv interpreter by absolute path. Task Scheduler starts jobs with
REM a bare environment and an unpredictable working directory, so nothing here
REM may depend on PATH, on an activated venv, or on the current folder.
REM
REM Run by hand for any past day:
REM     scripts\morning_report.cmd --date 2026-07-31 --open
REM ---------------------------------------------------------------------------
setlocal
set "REPO=%~dp0.."
set "PY=%REPO%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [morning_report] interpreter not found: "%PY%"
    exit /b 1
)

"%PY%" "%REPO%\scripts\run_morning_report.py" %*
exit /b %ERRORLEVEL%
