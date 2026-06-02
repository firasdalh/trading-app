# AI-Assisted Multi-Agent Trading App

A decision-support and (optionally) automation tool that analyzes trading pairs with a
team of AI agents, produces explainable trade proposals, runs them through a
**deterministic** risk module, and then either queues them for approval or executes them
— depending on the mode you select.

> This is **not** a guaranteed-profit machine. It is built to be disciplined, transparent,
> and safe. Backtest and paper results do not guarantee live results. Read
> [`RISK.md`](RISK.md) before changing any risk setting.

## Safety model (non-negotiable)

- **Paper trading is the default.** Live trading requires an explicit Settings switch plus a
  typed confirmation phrase, and re-confirmation on every restart.
- **The Risk Manager is deterministic code, never an LLM.** It can shrink or veto any
  proposal, and its decision is final.
- **Global kill-switch** (UI button + `KILL_SWITCH` env flag) halts all new orders.
- **Hard limits** (from `RISK.md`) are enforced server-side and cannot be weakened.
- **Every order is logged before and after submission** with the full agent reasoning.
- **API keys live only in `.env`** (gitignored). Never hard-coded, never logged.

## Project layout

```
trading-app/
  backend/    FastAPI + agents + risk + brokers + data + execution + backtest
  frontend/   React + Vite + TS + Tailwind + TradingView Lightweight Charts
  RISK.md     Authoritative risk limits — read before editing
```

## Backend — local dev (Milestone 1)

Requirements: Python 3.11+ (tested on 3.14).

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit .env — keep it out of git
uvicorn app.main:app --reload
```

Then open:

- `http://127.0.0.1:8000/health`        — liveness probe
- `http://127.0.0.1:8000/docs`          — interactive API docs
- `http://127.0.0.1:8000/api/settings`  — current app + risk settings (paper by default)

### What to test after Milestone 1

1. `GET /health` returns `{"status": "ok", ...}`.
2. `GET /api/settings` shows `execution_mode = "A_PROPOSE_APPROVE"`, `broker_env = "paper"`,
   and the risk defaults from `RISK.md`.
3. The SQLite DB file is created on first boot and the settings/risk rows are seeded.
4. `GET /api/risk/state` shows a fresh daily risk state (0 P&L, not paused).

## Milestones

1. ✅ Scaffold, config, secrets, DB models, FastAPI skeleton.
2. BrokerAdapter interface + Alpaca paper adapter + market data.
3. Deterministic Risk Manager + unit tests.
4. Technical Analyst → Orchestrator → TradeProposal (Mode A).
5. Frontend dashboard + chart + proposal panel + kill-switch.
6. Fundamental Analyst + news/calendar; Execution & Monitor; Modes B/C.
7. ccxt + OANDA adapters.
8. Backtesting module + UI.
9. Reflection/Journal agent + risk dashboard polish.
