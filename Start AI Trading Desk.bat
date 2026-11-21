@echo off
REM Opens the AI Trading Desk launcher window (launcher\TradingDesk.ps1). It starts MetaTrader 5, the backend and
REM the desk screen when they are not running, then opens the desk in Chrome. Same as the desktop shortcut.
REM 2026-11-21: replaces the old desktop.py launcher, which served a built UI (frontend\dist) that is no longer built.
start "" wscript.exe "%~dp0launcher\TradingDesk.vbs"
