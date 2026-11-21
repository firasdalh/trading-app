@echo off
REM Stops the AI Trading Desk: asks the backend to stop itself first - an order being sent finishes and is
REM recorded - then ends the backend and the desk screen. MetaTrader 5 stays open. Broker-side stop-loss and
REM take-profit stay, but nothing else manages the trades until the app is started again.
REM The launcher window's Stop button does the same. Batch rules for this file: ASCII only, CRLF line ends.
title Stop AI Trading Desk
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\TradingDesk.ps1" -Stop
echo.
pause
