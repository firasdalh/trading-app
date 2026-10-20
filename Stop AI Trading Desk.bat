@echo off
REM Stops the AI Trading Desk backend (the one the launcher leaves running in the background).
REM
REM Closing the app window does NOT stop it - the backend deliberately keeps running so it can go on
REM monitoring open positions. Use this when you actually want it off.
REM
REM NOTE: with it stopped nothing manages your trades - no stop-to-breakeven, no daily-loss pause,
REM no armed setups firing. Your broker-side stop-losses still stand, but nothing else does.
REM
REM Batch rules for this file: ASCII only, CRLF line ends, no parentheses inside a block's echo
REM (an unescaped ")" ends the block - this script never stopped anything until 2026-10-19).
setlocal
title Stop AI Trading Desk

netstat -ano | findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING" >nul
if errorlevel 1 (
    echo AI Trading Desk is not running.
    goto :done
)

REM 1. Ask the app to stop ITSELF. It stops starting new work and lets an order that is being sent
REM    finish and be recorded before it exits. Killing it outright could land mid order_send: a
REM    trade filled at the broker that the app never recorded. Full review 2026-10-20, API-10.
echo Asking AI Trading Desk to stop - any order being sent finishes first ...
curl -s -m 10 -X POST -H "X-Desk: 1" http://127.0.0.1:8001/api/app/shutdown >nul 2>&1

REM Wait up to ~30 s for the port to free. ping, not timeout: timeout needs an interactive console.
set /a TRIES=0
:wait
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING" >nul
if errorlevel 1 goto :stopped
set /a TRIES+=1
if %TRIES% LSS 15 goto :wait

REM 2. Still up: force it. /T takes the whole process tree - the venv pythonw.exe is only a launcher
REM    stub that starts the real interpreter as a child, so the listener and the stub are two processes.
echo It did not stop by itself - forcing it.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING"') do (
    echo Stopping PID %%P ...
    taskkill /PID %%P /T /F
)

REM Do not take taskkill's word for it - check the port is actually free.
ping -n 4 127.0.0.1 >nul
netstat -ano | findstr /R /C:"TCP.*127.0.0.1:8001.*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  *** COULD NOT STOP IT - the app is STILL RUNNING on port 8001. ***
    echo  Close this window, right-click "Stop AI Trading Desk.bat" and choose "Run as administrator".
    goto :done
)

:stopped
echo Stopped. Open positions are no longer being monitored by the app.

:done
echo.
pause
