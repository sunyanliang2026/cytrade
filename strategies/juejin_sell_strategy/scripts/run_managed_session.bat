@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0..\..\.."
set "REPO_ROOT=%CD%"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1

set "RUNNER_MODULE=strategies.juejin_sell_strategy.scripts.run_managed_session"

rem Double-click defaults to live. Use: run_managed_session.bat dryrun
if /I "%~1"=="dryrun" goto dryrun

rem This process-local setting does not modify config/local_runtime.json.
set "CYTRADE_JUEJIN_SELL_DRY_RUN=false"

echo Run Juejin sell strategy managed session [LIVE]...
echo Python: %PY%
echo Runner module: %RUNNER_MODULE%
echo CSV: %REPO_ROOT%\strategies\juejin_sell_strategy\data\sell_10.csv
echo.
echo Planned sell orders:
type "%REPO_ROOT%\strategies\juejin_sell_strategy\data\sell_10.csv"
echo.
echo [LIVE WARNING] This will connect the account and may send real sell orders.
set /p "ACK=Type 1 and press Enter to continue: "
if not "!ACK!"=="1" (
    echo Aborted.
    exit /b 0
)
echo.

"%PY%" -m %RUNNER_MODULE% --require-live --confirm-live
if errorlevel 1 goto failed

echo Done.
exit /b 0

:dryrun
shift
echo Run Juejin sell strategy managed session [DRY-RUN]...
echo Python: %PY%
echo Runner module: %RUNNER_MODULE%
echo.
"%PY%" -m %RUNNER_MODULE% %*
if errorlevel 1 goto failed
echo Done.
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
