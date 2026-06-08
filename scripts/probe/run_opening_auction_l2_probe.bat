@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%..\.."
set "CONDA_PYTHON=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe"
set "DEFAULT_POOL=%REPO_ROOT%\data\stock_pools\current\main_seal_follow_pool.csv"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"
set "OUT_DIR=%REPO_ROOT%\data\probe\opening_auction_l2\%TS%"
set "LOG_DIR=%REPO_ROOT%\logs\opening_auction_l2"
set "LOG_FILE=%LOG_DIR%\opening_auction_l2_%TS%.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if exist "%CONDA_PYTHON%" (
  set "PYTHON_EXE=%CONDA_PYTHON%"
) else (
  set "PYTHON_EXE=python"
)

echo [cytrade] python=%PYTHON_EXE%
echo [cytrade] output=%OUT_DIR%
echo [cytrade] log=%LOG_FILE%

pushd "%REPO_ROOT%"
if "%~1"=="" (
  echo [cytrade] args=--early-pool "%DEFAULT_POOL%"
  "%PYTHON_EXE%" "%REPO_ROOT%\scripts\probe\probe_opening_auction_l2.py" --output-dir "%OUT_DIR%" --log-file "%LOG_FILE%" --early-pool "%DEFAULT_POOL%"
) else (
  echo [cytrade] args=%*
  "%PYTHON_EXE%" "%REPO_ROOT%\scripts\probe\probe_opening_auction_l2.py" --output-dir "%OUT_DIR%" --log-file "%LOG_FILE%" %*
)
set "RC=%ERRORLEVEL%"
popd

echo.
echo [cytrade] completed rc=%RC%
echo [cytrade] tail log:
if exist "%LOG_FILE%" (
  powershell -NoProfile -Command "Get-Content -Path '%LOG_FILE%' -Encoding UTF8 -Tail 120"
) else (
  echo [cytrade] log file not found.
)

pause
exit /b %RC%
