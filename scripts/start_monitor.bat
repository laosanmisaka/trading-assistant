@echo off
setlocal
cd /d "%~dp0.."
if not exist outputs mkdir outputs
if not defined TRADING_ASSISTANT_PYTHON set "TRADING_ASSISTANT_PYTHON=python"
"%TRADING_ASSISTANT_PYTHON%" -u scripts\monitor_buy.py >> outputs\monitor.log 2>&1
exit /b %errorlevel%
