# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AI Trading Desk desktop app (one-folder build).

Build:  cd backend && python -m PyInstaller desktop.spec --noconfirm
Output: backend/dist/AI Trading Desk/  (run "AI Trading Desk.exe" inside it)

Heavy/lazy-imported packages are collected explicitly (MT5, pywebview's .NET backend, the web
stack, Anthropic). The built React UI (frontend/dist) is bundled and resolved via sys._MEIPASS in
app.main. Note: only the MT5 (Exness) + sim brokers are packaged; ccxt/Alpaca/OANDA are excluded to
keep the bundle small (they're not used here).
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

# Packages PyInstaller can't fully trace on its own (lazy imports, data files, compiled/.NET bits).
_COLLECT = [
    "webview", "clr_loader", "MetaTrader5",
    "uvicorn", "fastapi", "starlette", "pydantic", "pydantic_settings", "pydantic_core",
    "sqlalchemy", "apscheduler", "anthropic", "openai", "websockets", "httpx", "httpcore",
    "google.genai", "google.generativeai",  # Gemini (whichever is installed; missing ones skipped)
]

datas, binaries, hiddenimports = [], [], []
for pkg in _COLLECT:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # not installed — skip

# Our own package has many lazy `from app... import` calls; pull every submodule in.
hiddenimports += collect_submodules("app")
# Ship the built UI (served by app.main from sys._MEIPASS/frontend/dist).
datas += [("../frontend/dist", "frontend/dist")]

a = Analysis(
    ["desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "ccxt", "alpaca", "oandapyV20"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AI Trading Desk",
    console=True,          # keep a console for the first build so errors are visible
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AI Trading Desk")
