@echo off
setlocal

cd /d C:\Users\ysun\workspace\cytrade

set PY=C:\Users\ysun\miniconda3\envs\cytrade311\python.exe
set RUNNER_MODULE=strategies.juejin_sell_strategy.scripts.run_managed_session

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    exit /b 1
)

echo Run Juejin sell strategy managed session...
echo Python: %PY%
echo Runner module: %RUNNER_MODULE%
echo Args: %*

"%PY%" -m %RUNNER_MODULE% %*
if errorlevel 1 goto failed

echo Done.
exit /b 0

:failed
echo Failed. See logs above.
pause
exit /b 1
