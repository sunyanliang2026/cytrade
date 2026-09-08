@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Edit these parameters before double-clicking this BAT.
rem RUN_LIVE=false means dry-run/mock only.
rem RUN_LIVE=true sends real orders after frozen-plan confirmation.
rem CSV_FILE is the stock pool. It must contain only: stock_code,amount.
rem Add as many rows as required in that CSV.
rem ============================================================
set "SUBMIT_TIME=08:30:00"
set "RUN_LIVE=true"
set "NO_WAIT=false"
set "MARKET_DAY_ONLY=true"
set "POST_SUBMIT_WAIT_SEC=10"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..\..") do set "REPO_ROOT=%%~fI"
set "CSV_FILE=%REPO_ROOT%\strategies\overnight_limit_up_buy\data\orders.csv"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 goto failed

set "RUNNER_MODULE=strategies.overnight_limit_up_buy.scripts.run_overnight_limit_up_buy"
set "DRY_STATE_FILE=%REPO_ROOT%\strategies\overnight_limit_up_buy\state\bat_dry_submitted_orders.json"
set "LIVE_STATE_FILE=%REPO_ROOT%\strategies\overnight_limit_up_buy\state\bat_live_submitted_orders.json"

if not exist "%CSV_FILE%" (
    echo CSV file not found: %CSV_FILE%
    goto failed
)

set "STATE_FILE=%DRY_STATE_FILE%"
set "LIVE_ARGS="
if /I "%RUN_LIVE%"=="true" (
    set "STATE_FILE=%LIVE_STATE_FILE%"
    set "LIVE_ARGS=--live --confirm-live"
)

set "WAIT_ARGS="
if /I "%NO_WAIT%"=="true" set "WAIT_ARGS=--no-wait"

set "MARKET_DAY_ARGS=--market-day-only"
if /I "%MARKET_DAY_ONLY%"=="false" set "MARKET_DAY_ARGS=--no-market-day-only"

echo ============================================================
echo Overnight limit-up buy
echo Repo:        %REPO_ROOT%
echo Python:      %CYTRADE_PYTHON%
echo Submit time: %SUBMIT_TIME%
echo Run live:    %RUN_LIVE%
echo CSV file:    %CSV_FILE%
echo State file:  %STATE_FILE%
echo ============================================================
echo.
echo CSV stock pool to be processed:
type "%CSV_FILE%"
echo.
set /p "POOL_ACK=Confirm this CSV stock pool? Type 1 and press Enter: "
if not "!POOL_ACK!"=="1" (
    echo Aborted.
    goto failed
)

pushd "%REPO_ROOT%"
"%CYTRADE_PYTHON%" -m %RUNNER_MODULE% ^
  --csv "%CSV_FILE%" ^
  --state-file "%STATE_FILE%" ^
  --submit-time "%SUBMIT_TIME%" ^
  --post-submit-wait-sec "%POST_SUBMIT_WAIT_SEC%" ^
  --require-plan-confirm ^
  %MARKET_DAY_ARGS% ^
  %WAIT_ARGS% ^
  %LIVE_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" goto failed

echo.
echo Done.
pause
exit /b 0

:failed
echo.
echo Failed or aborted. See console output and logs above.
pause
exit /b 1
