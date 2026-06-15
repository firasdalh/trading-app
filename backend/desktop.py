"""Desktop launcher — run the AI Trading Desk as a native app window.

Starts the FastAPI backend (which also serves the built React UI) on a local port, waits for it
to come up, then shows it in a native window via pywebview. If pywebview isn't available (e.g. its
Windows backend has no wheel for this Python yet), it falls back to opening the app in the
browser's chromeless "app mode" instead — so the launcher always works.

Run it via the "Start AI Trading Desk" launcher at the repo root, or:  python desktop.py

Prerequisites:
  - the frontend is built  ->  cd frontend && npm run build   (creates frontend/dist)
  - the MetaTrader 5 terminal is running + logged in (the app connects to it)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
PORT = 8001
URL = f"http://{HOST}:{PORT}"


def _run_server() -> None:
    """Run uvicorn in-process. Signal handlers are skipped because we may not be on the main
    thread (uvicorn only installs them on the main thread)."""
    import uvicorn

    config = uvicorn.Config("app.main:app", host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server.run()


def _ping(timeout: float = 1.0) -> bool:
    """True if a server is already answering on the port (so we don't start a second one)."""
    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _wait_until_up(timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ping():
            return True
        time.sleep(0.25)
    return False


def _open_in_browser_when_up() -> None:
    """Fallback path: once the server is up, open the app in a chromeless Edge/Chrome window
    (looks like an app), or the default browser if neither is found."""
    if not _wait_until_up():
        return
    for exe in ("msedge", "chrome", "chromium"):
        path = shutil.which(exe)
        if path:
            try:
                subprocess.Popen([path, f"--app={URL}", "--window-size=1480,940"])
                return
            except Exception:  # noqa: BLE001
                pass
    webbrowser.open(URL)


def _ensure_data_dir() -> None:
    """In a frozen build the program folder is read-only, so put the DB + .env in a persistent,
    writable per-user location (%APPDATA%\\AITradingDesk) by making it the working directory before
    the app (which uses ./trading.sqlite3 and ./.env) is imported. No-op when running from source."""
    if not getattr(sys, "frozen", False):
        return
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    data_dir = os.path.join(base, "AITradingDesk")
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)


def main() -> None:
    _ensure_data_dir()  # must run BEFORE the server imports app.main (reads ./.env, ./db)
    try:
        import webview  # pywebview — native window
    except Exception:  # noqa: BLE001 - backend/GUI lib not available on this Python
        webview = None

    # Reuse an already-running server (dev, or a second launch) instead of failing to bind the port.
    server_needed = not _ping()

    if webview is not None:
        # Native window: server in a background thread, window on the main thread. Closing the
        # window returns from start(), the daemon server thread dies, the process exits.
        if server_needed:
            threading.Thread(target=_run_server, daemon=True).start()
            _wait_until_up()
        webview.create_window("AI Trading Desk", URL, width=1480, height=940, min_size=(1100, 700))
        webview.start()
    elif server_needed:
        # Fallback: open the browser app-window once up, and run the server in the FOREGROUND so
        # the process stays alive (closing the console window stops everything).
        threading.Thread(target=_open_in_browser_when_up, daemon=True).start()
        _run_server()
    else:
        # No window lib and a server is already running — just open the app window and exit.
        _open_in_browser_when_up()


if __name__ == "__main__":
    main()
