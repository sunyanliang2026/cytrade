@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "REPO_ROOT=%%~fI"

if not defined CYTRADE_PYTHON (
    set "CYTRADE_PYTHON=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe"
)

if not exist "%CYTRADE_PYTHON%" (
    echo [ERROR] Python not found: %CYTRADE_PYTHON%
    exit /b 1
)

pushd "%REPO_ROOT%"
"%CYTRADE_PYTHON%" -m strategies.main_seal_follow.scripts.run_managed_session %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
