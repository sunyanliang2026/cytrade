@echo off
setlocal
cd /d "%~dp0..\..\.."
call "%CD%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1
"%PY%" -m strategies.large_order_limit_up_buy.scripts.run_market_only %*
exit /b %ERRORLEVEL%
