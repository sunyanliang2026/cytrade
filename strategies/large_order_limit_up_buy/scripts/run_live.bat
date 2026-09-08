@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Edit this section before running.
rem RUN_LIVE=false uses market-only dry-run and never connects account.
rem RUN_LIVE=true requires a second console confirmation before live monitoring.
rem The live pool can contain multiple code,plan_amount rows.
rem ============================================================
set "RUN_LIVE=true"
set "POOL_FILE=strategies\large_order_limit_up_buy\data\manual_pool.csv"
set "STOP_TIME=15:00"
set "MAX_ORDER_AMOUNT=100000"
set "MAX_TOTAL_AMOUNT=1000000"
set "NEIGHBOR_COUNT=10"
set "NEIGHBOR_WINDOW_SECONDS=3"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..\..") do set "REPO_ROOT=%%~fI"
call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 goto failed

pushd "%REPO_ROOT%"
if /I "%RUN_LIVE%"=="true" (
    echo ============================================================
    echo [LIVE] Large-order limit-up buy
    echo Pool:       %POOL_FILE%
    echo Max order:  %MAX_ORDER_AMOUNT%
    echo Max total:  %MAX_TOTAL_AMOUNT%
    echo Stop time:  %STOP_TIME%
    echo ============================================================
    echo Planned orders from CSV:
    type "%REPO_ROOT%\%POOL_FILE%"
    echo ============================================================
    echo [LIVE WARNING] A qualifying Level2 limit-up buy order can send a real order.
    set /p "ACK=Type 1 and press Enter to connect account and continue: "
    if not "!ACK!"=="1" (
        echo Aborted.
        goto done
    )
    "%PY%" -m strategies.large_order_limit_up_buy.scripts.run_live ^
      --live --confirm-live ^
      --pool "%POOL_FILE%" ^
      --stop-time "%STOP_TIME%" ^
      --max-order-amount "%MAX_ORDER_AMOUNT%" ^
      --max-total-amount "%MAX_TOTAL_AMOUNT%" ^
      --neighbor-count "%NEIGHBOR_COUNT%" ^
      --neighbor-window-seconds "%NEIGHBOR_WINDOW_SECONDS%"
) else (
    echo [DRY_RUN] RUN_LIVE is false. Account will not be connected.
    "%PY%" -m strategies.large_order_limit_up_buy.scripts.run_market_only ^
      --pool "%POOL_FILE%" ^
      --stop-time "%STOP_TIME%" ^
      --neighbor-count "%NEIGHBOR_COUNT%" ^
      --neighbor-window-seconds "%NEIGHBOR_WINDOW_SECONDS%"
)
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" goto failed

:done
echo Done.
pause
exit /b 0

:failed
echo Failed. Check the console and logs above.
pause
exit /b 1
