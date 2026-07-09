@echo off
rem Shared runtime environment for project BAT entry points.

if not defined REPO_ROOT (
    for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
)

if not defined CYTRADE_PYTHON (
    if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
        set "CYTRADE_PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
    )
)

if not defined CYTRADE_PYTHON (
    set "CYTRADE_PYTHON=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe"
)

if not exist "%CYTRADE_PYTHON%" (
    echo [ERROR] Python not found: %CYTRADE_PYTHON%
    exit /b 1
)

set "PY=%CYTRADE_PYTHON%"
exit /b 0
