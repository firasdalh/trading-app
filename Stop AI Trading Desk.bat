@echo off
REM Stops the AI Trading Desk backend (the one the launcher leaves running in the background).
REM
REM Closing the app window does NOT stop it — the backend deliberately keeps running so it can go on
REM monitoring open positions. Use this when you actually want it off.
REM
REM NOTE: with it stopped nothing manages your trades — no stop-to-breakeven, no daily-loss pause,
REM no armed setups firing. Your broker-side stop-losses still stand, but nothing else does.
setlocal
title Stop AI Trading Desk

REM Find whatever is listening on the app's port and stop it (matches PORT in backend/desktop.py).
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING"') do (
    set "FOUND=1"
    echo Stopping AI Trading Desk (PID %%P)...
    taskkill /PID %%P /F >nul 2>&1
)

if not defined FOUND (
    echo AI Trading Desk is not running.
) else (
    echo Stopped. Open positions are no longer being monitored by the app.
)
echo.
pause
