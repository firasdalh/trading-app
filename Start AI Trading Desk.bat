@echo off
REM One-click launcher for the AI Trading Desk desktop app.
REM Starts the backend (which serves the built UI) and opens it in an app window.
REM First time / after a UI change, build the frontend once:  cd frontend ^&^& npm run build
cd /d "%~dp0backend"
title AI Trading Desk
echo Starting AI Trading Desk... (this window keeps it running; close it to quit)
".venv\Scripts\python.exe" desktop.py
