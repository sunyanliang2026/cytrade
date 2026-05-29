@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

if not defined CYTRADE_PYTHON (
    set "CYTRADE_PYTHON=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe"
)

if not exist "%CYTRADE_PYTHON%" (
    echo [ERROR] Python not found: %CYTRADE_PYTHON%
    exit /b 1
)

pushd "%REPO_ROOT%"
"%CYTRADE_PYTHON%" "scripts\run_main_seal_follow_monitor_session.py" --pool-output "config\main_seal_follow_manual_pool.csv" --skip-pool-collect %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
