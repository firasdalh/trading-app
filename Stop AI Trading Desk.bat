@echo off
REM Stops the AI Trading Desk backend (the one the launcher leaves running in the background).
REM
REM Closing the app window does NOT stop it - the backend deliberately keeps running so it can go on
REM monitoring open positions. Use this when you actually want it off.
REM
REM NOTE: with it stopped nothing manages your trades - no stop-to-breakeven, no daily-loss pause,
REM no armed setups firing. Your broker-side stop-losses still stand, but nothing else does.
setlocal
title Stop AI Trading Desk

REM Find whatever is listening on the app's port and stop it (matches PORT in backend/desktop.py).
REM /T takes the whole process tree: on Windows the venv's pythonw.exe is only a launcher stub that
REM starts the real interpreter as a child, so the listener and the stub are two processes.
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING"') do (
    set "FOUND=1"
    REM No parentheses in this echo: inside a "do ( ... )" block an unescaped ")" ends the block,
    REM so the old "(PID %%P)" made cmd abort with "... was unexpected at this time." before
    REM taskkill ever ran. Double-clicked, that error flashed and the window closed - which looked
    REM exactly like success. This script never stopped anything until 2026-10-19.
    echo Stopping AI Trading Desk - PID %%P ...
    taskkill /PID %%P /T /F
)

if not defined FOUND (
    echo AI Trading Desk is not running.
    goto :done
)

REM Do not take taskkill's word for it - check the port is actually free. This script used to hide
REM taskkill's output and print "Stopped" regardless, so on 2026-10-19 a backend from Oct 14 survived a
REM restart and kept serving old code while everything on screen said it had stopped.
REM ~3 s wait. ping, not timeout: timeout refuses to run without an interactive console.
ping -n 4 127.0.0.1 >nul
netstat -ano | findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  *** COULD NOT STOP IT - the app is STILL RUNNING on port 8001. ***
    echo  Close this window, right-click "Stop AI Trading Desk.bat" and choose "Run as administrator".
) else (
    echo Stopped. Open positions are no longer being monitored by the app.
)

:done
echo.
pause
