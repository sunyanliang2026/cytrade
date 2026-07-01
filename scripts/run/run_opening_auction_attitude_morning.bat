@echo off
setlocal

cd /d C:\Users\ysun\workspace\cytrade

set PY=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
set POOL=data\stock_pools\current\opening_auction_universe.csv
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set RUN_ID=%%i
set PROBE_DIR=data\probe\opening_auction_l2\%RUN_ID%
set SNAPSHOT_OUTPUT=%PROBE_DIR%\snapshot_full_pool.jsonl
set RANKING_OUTPUT=%PROBE_DIR%\auction_rankings.csv
set BUY_PLAN_OUTPUT=%PROBE_DIR%\auction_buy_plan.csv
set MATCHED_CANDIDATES_OUTPUT=%PROBE_DIR%\auction_matched_candidates.csv
set MATCHED_CANDIDATES_MD_OUTPUT=%PROBE_DIR%\auction_matched_candidates.md
set MANIFEST_OUTPUT=%PROBE_DIR%\run_manifest.json

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    exit /b 1
)

echo [1/5] Collect source stock pool and write source cache...
"%PY%" -m scripts.pool.collect_main_seal_pool --once --source combined --amount 50000
if errorlevel 1 goto failed

echo [2/5] Build strict opening auction universe...
"%PY%" -m scripts.pool.build_opening_auction_universe --strict --output "%POOL%"
if errorlevel 1 goto failed

echo [3/5] Start all-candidate L2 market data recorder...
echo Probe output: %PROBE_DIR%
start "OpeningAuctionL2Probe" /min cmd /c ""%PY%" scripts\probe\probe_opening_auction_l2.py --early-pool "%POOL%" --output-dir "%PROBE_DIR%" --early-subscribe-at 09:15:00 --capture-start 09:15:00 --capture-end 09:35:00 --final-10s-start 09:24:50 --final-10s-end 09:25:05 --open-5m-start 09:30:00 --open-5m-end 09:35:00 --stop-at 09:35:00 --log-file "%PROBE_DIR%\probe_console.log""

echo [4/5] Run all-candidate opening auction strategy and full-pool snapshot recorder...
"%PY%" scripts\run\run_opening_auction_attitude_market_only.py --pool "%POOL%" --install-all --scan-start-time 09:15:00 --snapshot-interval-sec 2 --snapshot-record-path "%SNAPSHOT_OUTPUT%" --ranking-output "%RANKING_OUTPUT%" --buy-plan-output "%BUY_PLAN_OUTPUT%" --matched-candidates-output "%MATCHED_CANDIDATES_OUTPUT%" --matched-candidates-md-output "%MATCHED_CANDIDATES_MD_OUTPUT%" --preopen-reference-time 09:25:15 --stop-time 09:35:00 --heartbeat-interval-sec 10
if errorlevel 1 goto failed

echo [5/5] Write run manifest...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$manifest=[ordered]@{run_id='%RUN_ID%';strategy='opening_auction_attitude';mode='install_all_full_candidate_pool';pool='%POOL%';probe_dir='%PROBE_DIR%';l2_raw=(Join-Path '%PROBE_DIR%' 'opening_l2_raw.jsonl');l2_summary=(Join-Path '%PROBE_DIR%' 'opening_l2_summary.csv');l2_schema=(Join-Path '%PROBE_DIR%' 'opening_l2_schema.json');probe_console_log=(Join-Path '%PROBE_DIR%' 'probe_console.log');snapshot_full_pool='%SNAPSHOT_OUTPUT%';ranking_output='%RANKING_OUTPUT%';buy_plan_output='%BUY_PLAN_OUTPUT%';matched_candidates_output='%MATCHED_CANDIDATES_OUTPUT%';matched_candidates_md_output='%MATCHED_CANDIDATES_MD_OUTPUT%';preopen_reference_time='09:25:15';observe_only=$true;real_order_sent=$false}; $manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 '%MANIFEST_OUTPUT%'"
if errorlevel 1 goto failed

echo Done.
echo Manifest: %MANIFEST_OUTPUT%
echo Probe output: %PROBE_DIR%
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
