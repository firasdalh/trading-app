@echo off
REM One-click launcher for the AI Trading Desk desktop app.
REM Starts the backend (which serves the built UI) and opens it in a chromeless app window.
REM First time / after a UI change, build the frontend once:  cd frontend ^&^& npm run build
REM
REM Runs under PYTHONW (not python), so there is NO console window. The backend then keeps running
REM in the background after you close the app window. That is deliberate: it is what monitors open
REM positions, moves stops to breakeven, honours the daily-loss breaker and fires armed setups.
REM Stopping it with the window would leave live trades unmanaged.
REM
REM Re-run this any time to reopen the window — it reuses the backend already running.
REM To actually stop everything, use "Stop AI Trading Desk.bat".
cd /d "%~dp0backend"
start "" ".venv\Scripts\pythonw.exe" desktop.py
