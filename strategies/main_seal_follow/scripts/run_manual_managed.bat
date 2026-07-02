@echo off
setlocal

call "%~dp0run_managed_session.bat" --pool-output "data\stock_pools\manual\main_seal_follow_manual_pool.csv" --skip-pool-collect %*
exit /b %ERRORLEVEL%
