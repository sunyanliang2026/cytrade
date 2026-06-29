@echo off
setlocal

cd /d C:\Users\ysun\workspace\cytrade

set PY=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
set RUNNER=scripts\run\run_juejin_sell_strategy_managed_session.py

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    exit /b 1
)

if not exist "%RUNNER%" (
    echo [ERROR] Runner not found: %RUNNER%
    exit /b 1
)

echo Run Juejin sell strategy managed session...
echo Python: %PY%
echo Runner: %RUNNER%
echo Args: %*

"%PY%" "%RUNNER%" %*
if errorlevel 1 goto failed

echo Done.
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
