@echo off
REM Double-click to open the dashboard (Windows). Replaces Notices Dashboard.vbs.
REM
REM Everything beyond finding a Python lives in lib\serve.py --detach: it
REM backgrounds itself with no console window, logs to data\serve.log, opens
REM the browser, and exits once the dashboard is closed.

setlocal
set "HERE=%~dp0"

REM A venv if there is one, else whatever is on PATH. pythonw first in each
REM pair so no console flashes up; serve.py --detach re-checks anyway.
set "PY="
for %%P in (
    "%USERPROFILE%\.venvs\base\Scripts\pythonw.exe"
    "%USERPROFILE%\.venvs\base\Scripts\python.exe"
) do if not defined PY if exist %%P set "PY=%%~P"

if not defined PY for %%P in (pythonw.exe python.exe) do (
    if not defined PY for /f "delims=" %%F in ('where %%P 2^>nul') do (
        if not defined PY set "PY=%%F"
    )
)

if not defined PY (
    echo No Python found.
    echo.
    echo Install Python from python.org ^(tick "Add python.exe to PATH"^),
    echo or edit this file to point at your install.
    pause
    exit /b 1
)

"%PY%" "%HERE%lib\serve.py" --detach %*
