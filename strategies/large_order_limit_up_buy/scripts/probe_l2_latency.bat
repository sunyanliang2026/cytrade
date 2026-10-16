@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0..\..\.."
call "%CD%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1
set "PYTHONIOENCODING=utf-8"
"%PY%" -m strategies.large_order_limit_up_buy.scripts.probe_l2_latency %*
exit /b %ERRORLEVEL%
