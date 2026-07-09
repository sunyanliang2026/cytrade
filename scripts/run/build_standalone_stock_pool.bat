@echo off
setlocal

cd /d "%~dp0..\.."
set "REPO_ROOT=%CD%"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1

"%PY%" -m scripts.pool.build_standalone_stock_pool %*
exit /b %ERRORLEVEL%
