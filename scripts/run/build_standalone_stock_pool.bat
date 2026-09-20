@echo off
setlocal

cd /d "%~dp0..\.."
set "REPO_ROOT=%CD%"

call "%REPO_ROOT%\scripts\run\load_runtime_env.bat"
if errorlevel 1 exit /b 1

"%PY%" -m scripts.pool.build_standalone_stock_pool %*
if errorlevel 1 exit /b %ERRORLEVEL%

set "SOURCE_POOL=%REPO_ROOT%\data\standalone_stock_pool\jingjiabuy.csv"
set "TARGET_DIR=C:\Users\ysun\.ydgm3\projects\f9264b2a-f106-11f0-8a04-e4b97a6af28c"
set "TARGET_POOL=%TARGET_DIR%\jingjiabuy.csv"

if not exist "%SOURCE_POOL%" (
    echo [ERROR] Generated stock pool not found: "%SOURCE_POOL%"
    exit /b 1
)

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if errorlevel 1 exit /b %ERRORLEVEL%

copy /Y "%SOURCE_POOL%" "%TARGET_POOL%"
if errorlevel 1 exit /b %ERRORLEVEL%

echo [OK] Copied stock pool to "%TARGET_POOL%"
exit /b 0
