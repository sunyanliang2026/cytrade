@echo off
setlocal

cd /d "%~dp0..\..\.."
set "REPO_ROOT=%CD%"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1

set POOL=strategies\opening_auction_attitude\data\opening_auction_universe.csv
set SOURCE_CONFIG=strategies\opening_auction_attitude\config\opening_auction_pool_sources.json
set SOURCE_COLLECT_OUTPUT=strategies\opening_auction_attitude\data\opening_auction_source_pool.csv
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set RUN_ID=%%i
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set RUN_DATE=%%i
set PROBE_DIR=data\probe\opening_auction_l2\%RUN_ID%
set SOURCE_CACHE_ROOT=strategies\opening_auction_attitude\data\source_cache
set SOURCE_CACHE_DIR=%SOURCE_CACHE_ROOT%\%RUN_DATE%
set SNAPSHOT_OUTPUT=%PROBE_DIR%\snapshot_full_pool.jsonl
set RANKING_OUTPUT=%PROBE_DIR%\auction_rankings.csv
set BUY_PLAN_OUTPUT=%PROBE_DIR%\auction_buy_plan.csv
set MATCHED_CANDIDATES_OUTPUT=%PROBE_DIR%\auction_matched_candidates.csv
set MATCHED_CANDIDATES_MD_OUTPUT=%PROBE_DIR%\auction_matched_candidates.md
set L2_HEALTH_OUTPUT=%PROBE_DIR%\l2_subscription_health.csv
set MANIFEST_OUTPUT=%PROBE_DIR%\run_manifest.json
set CANDIDATE_FREEZE_TIME=09:24:45
set CANDIDATE_MIN_AUCTION_AMOUNT=5000000
set CANDIDATE_MIN_OPEN_PCT=1
set SMALL_POOL_L2_KINDS=l2order,l2transaction

echo [1/4] Collect source stock pool and write source cache...
"%PY%" -m scripts.pool.collect_main_seal_pool --once --source combined --source-config "%SOURCE_CONFIG%" --source-cache-dir "%SOURCE_CACHE_ROOT%" --output "%SOURCE_COLLECT_OUTPUT%" --trace-dir "strategies\opening_auction_attitude\output\stock_pool_runs" --amount 50000
if errorlevel 1 (
    echo [WARN] Source stock pool collection failed.
    if exist "%SOURCE_CACHE_DIR%\*.csv" (
        echo [WARN] Reusing existing source cache: %SOURCE_CACHE_DIR%
    ) else (
        echo [ERROR] No same-day source cache found: %SOURCE_CACHE_DIR%
        goto failed
    )
)

echo [2/4] Build strict opening auction universe...
"%PY%" -m scripts.pool.build_opening_auction_universe --strict --source-cache-dir "%SOURCE_CACHE_ROOT%" --output "%POOL%"
if errorlevel 1 goto failed

echo [3/4] Run full-pool snapshot scanner and small-pool L2 order/transaction observer...
echo Output: %PROBE_DIR%
"%PY%" -m strategies.opening_auction_attitude.scripts.run_market_only --pool "%POOL%" --dynamic-candidates --scan-start-time 09:15:00 --candidate-freeze-time %CANDIDATE_FREEZE_TIME% --candidate-min-auction-amount %CANDIDATE_MIN_AUCTION_AMOUNT% --candidate-min-open-pct %CANDIDATE_MIN_OPEN_PCT% --snapshot-interval-sec 2 --snapshot-record-path "%SNAPSHOT_OUTPUT%" --small-pool-l2-record-dir "%PROBE_DIR%" --ranking-output "%RANKING_OUTPUT%" --buy-plan-output "%BUY_PLAN_OUTPUT%" --matched-candidates-output "%MATCHED_CANDIDATES_OUTPUT%" --matched-candidates-md-output "%MATCHED_CANDIDATES_MD_OUTPUT%" --preopen-reference-time 09:25:15 --stop-time 09:30:30 --heartbeat-interval-sec 10
if errorlevel 1 goto failed

echo [4/4] Write run manifest...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$manifest=[ordered]@{run_id='%RUN_ID%';strategy='opening_auction_attitude';mode='full_pool_snapshot_scan_dynamic_small_pool_l2_order_transaction';pool='%POOL%';source_config='%SOURCE_CONFIG%';source_cache_root='%SOURCE_CACHE_ROOT%';source_collect_output='%SOURCE_COLLECT_OUTPUT%';probe_dir='%PROBE_DIR%';l2_raw=(Join-Path '%PROBE_DIR%' 'opening_l2_raw.jsonl');l2_summary=(Join-Path '%PROBE_DIR%' 'opening_l2_summary.csv');l2_health=(Join-Path '%PROBE_DIR%' 'l2_subscription_health.csv');l2_schema=(Join-Path '%PROBE_DIR%' 'opening_l2_schema.json');snapshot_full_pool='%SNAPSHOT_OUTPUT%';ranking_output='%RANKING_OUTPUT%';buy_plan_output='%BUY_PLAN_OUTPUT%';matched_candidates_output='%MATCHED_CANDIDATES_OUTPUT%';matched_candidates_md_output='%MATCHED_CANDIDATES_MD_OUTPUT%';candidate_freeze_time='%CANDIDATE_FREEZE_TIME%';candidate_min_auction_amount=[double]'%CANDIDATE_MIN_AUCTION_AMOUNT%';candidate_min_open_pct=[double]'%CANDIDATE_MIN_OPEN_PCT%';small_pool_l2_kinds='%SMALL_POOL_L2_KINDS%';preopen_reference_time='09:25:15';observe_only=$true;real_order_sent=$false}; $manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 '%MANIFEST_OUTPUT%'"
if errorlevel 1 goto failed

echo Done.
echo Manifest: %MANIFEST_OUTPUT%
echo Output: %PROBE_DIR%
exit /b 0

:failed
echo Failed. See logs above.
exit /b 1
