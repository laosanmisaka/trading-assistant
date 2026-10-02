@echo off
rem 每个交易日 09:25 由计划任务 TradingAssistant-Monitor 调用：
rem 1) 清掉昨天残留的 monitor_buy 进程（它非交易时段只是睡眠，永不自退）；
rem 2) 以最新 watchlist 启动今日盘中监控（控制台窗口保留，气泡通知照常）。
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*monitor_buy.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
cd /d %~dp0..
start "TA-Monitor" "C:\Users\yaowenwu\.workbuddy\binaries\python\envs\default\Scripts\python.exe" scripts\monitor_buy.py
