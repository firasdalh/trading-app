"""Run the backend's uvicorn (with --reload) from the launcher, its output in logs/backend.log.

Started HIDDEN by launcher/TradingDesk.ps1. Two Windows details make this file necessary:

- uvicorn's reload restarts the worker with a Ctrl+C plus a write to the reloader's stdout. With the process's
  std handles redirected to a file - or sys.stdout pointed at one in the reloader - that Ctrl+C is never processed
  and the reload hangs forever (tested 2026-11-21 on a throwaway server). So the reloader keeps stdout on its
  hidden console and only stderr (where uvicorn and the app log) goes to the file.
- A process can inherit "ignore Ctrl+C" from whatever started it; switched back on here, before uvicorn starts,
  so the reload worker (a child) inherits it ON.

Top-level code, not only under __main__: multiprocessing starts the reload worker by re-running this file as
__mp_main__, so the worker sends its output to the same log.
"""
import os
import sys

# Outside OneDrive (2026-11-22): OneDrive re-uploaded the growing log all the time.
LOG = os.environ.get("TRADING_DESK_LOG") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                                         "AITradingDesk", "logs", "backend.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
_log = open(LOG, "a", encoding="utf-8", buffering=1)
sys.stderr = _log
if __name__ != "__main__":
    sys.stdout = _log      # the reload worker; the reloader keeps its console stdout (see above)

if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    from uvicorn.main import main

    sys.argv = ["uvicorn"] + sys.argv[1:]
    main()
