@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1

pushd "%REPO_ROOT%"
"%CYTRADE_PYTHON%" -m agent.loops.post_morning_review %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
