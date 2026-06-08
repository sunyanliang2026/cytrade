@echo off
setlocal

cd /d C:\Users\ysun\workspace\cytrade

set PY=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
set POOL=data\stock_pools\current\opening_auction_universe.csv
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set RUN_ID=%%i
set PROBE_DIR=data\probe\opening_auction_l2\%RUN_ID%

echo [1/4] Collect source stock pool...
"%PY%" -m scripts.pool.collect_main_seal_pool --once --source combined --amount 50000
if errorlevel 1 goto failed

echo [2/4] Build opening auction universe...
"%PY%" -m scripts.pool.build_opening_auction_universe --strict
if errorlevel 1 goto failed

echo [3/4] Start market data recorder...
echo Probe output: %PROBE_DIR%
start "OpeningAuctionL2Probe" /min cmd /c ""%PY%" scripts\probe\probe_opening_auction_l2.py --early-pool "%POOL%" --output-dir "%PROBE_DIR%" --early-subscribe-at 09:15:00 --capture-start 09:15:00 --capture-end 09:35:00 --final-10s-start 09:24:50 --final-10s-end 09:25:05 --open-5m-start 09:30:00 --open-5m-end 09:35:00 --stop-at 09:35:00 --log-file "%PROBE_DIR%\probe_console.log""

echo [4/4] Run opening auction attitude scanner...
"%PY%" scripts\run\run_opening_auction_attitude_market_only.py --pool "%POOL%" --scan-start-time 09:15:00 --candidate-freeze-time 09:24:30 --snapshot-interval-sec 2 --snapshot-record-path "%PROBE_DIR%\snapshot_scan.jsonl" --stop-time 09:35:00 --heartbeat-interval-sec 10
if errorlevel 1 goto failed

echo Done.
echo Probe output: %PROBE_DIR%
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
