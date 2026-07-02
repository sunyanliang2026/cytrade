@echo off
setlocal

cd /d "%~dp0..\..\.."

set PY=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
set RUNNER_MODULE=strategies.juejin_sell_strategy.scripts.run_managed_session
set RUN_ARGS=%*

if /I "%~1"=="live" (
    set RUN_ARGS=--require-live --confirm-live
)

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    exit /b 1
)

echo Run Juejin sell strategy managed session...
echo Python: %PY%
echo Runner module: %RUNNER_MODULE%
echo Args: %RUN_ARGS%
echo.
echo Live mode note:
echo   This BAT never changes config/local_runtime.json.
echo   To run live, set CYTRADE_JUEJIN_SELL_DRY_RUN=false manually,
echo   then run this BAT with: live
echo   Equivalent explicit args: --require-live --confirm-live
echo.

"%PY%" -m %RUNNER_MODULE% %RUN_ARGS%
if errorlevel 1 goto failed

echo Done.
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
