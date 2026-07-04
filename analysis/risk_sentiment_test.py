"""Test the RISK-SENTIMENT (risk-on/off regime) filter — an orthogonal signal.

Idea: some trades are 'risk-on bets' (long equities/oil/AUD/NZD/CAD, short JPY/CHF/gold) and some are
'risk-off bets'. When the broad risk barometer (S&P 500 trend) disagrees with the bet, the trade is
fighting the macro tide. This checks whether trades ALIGNED with the risk regime beat trades that
FIGHT it — if so, skipping the fighters is a real, independent filter.

Mostly offline: classifies each entries.json trade via the exposure-factor model; the only live data
is the S&P daily trend (one fetch). Run with uvicorn stopped:
    PYTHONPATH=backend python analysis/risk_sentiment_test.py
"""
from __future__ import annotations

import bisect
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.risk.correlation import exposure_factors

HERE = Path(__file__).parent
ENTRIES = HERE / "entries.json"
OUT = HERE / "risk_sentiment_test.md"

# Which risk factors are 'risk-on' (+1) vs 'safe-haven' (-1). USD is deliberately excluded (ambiguous).
RISK_ON = {"EQUITY": 1, "CRYPTO": 1, "ENERGY": 1, "AUD": 1, "NZD": 1, "CAD": 1,
           "JPY": -1, "CHF": -1, "METAL": -1}


def _ema(vals, p):
    if len(vals) < p:
        return None
    k = 2 / (p + 1)
    e = sum(vals[:p]) / p
    for v in vals[p:]:
        e = v * k + e * (1 - k)
    return e


def risk_bet(symbol, direction):
    """>0 = the trade profits in RISK-ON; <0 = profits in RISK-OFF; 0 = neutral."""
    f = exposure_factors(symbol, direction)
    return sum(RISK_ON.get(k, 0) * v for k, v in f.items())


def build_regime():
    """Daily risk-on(+1)/off(-1) timeline from the S&P 500 (EMA20 vs EMA50). Returns (dates, states)."""
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    s.close()
    from app.models.enums import AssetClass
    candles = None
    for sym in ("US500m", "US500", "SPX500m", "USTECm"):   # fall back to Nasdaq if S&P absent
        try:
            sd = get_broker_for(AssetClass.INDEX, bmap).get_ohlcv(sym, "1d", limit=500)
            if sd and sd.candles:
                candles = sd.candles; used = sym; break
        except Exception:  # noqa: BLE001
            continue
    if not candles:
        return None, None, None
    dates = [c.ts.date() for c in candles]
    closes = [c.close for c in candles]
    states = []
    for i in range(len(closes)):
        w = closes[: i + 1]
        e20, e50 = _ema(w, 20), _ema(w, 50)
        states.append(1 if (e20 and e50 and e20 > e50) else (-1 if (e20 and e50) else 0))
    return dates, states, used


def regime_at(dates, states, when):
    d = when.date()
    i = bisect.bisect_right(dates, d) - 1
    return states[i] if 0 <= i < len(states) else 0


def stats(rs):
    a = np.array(rs, dtype=float)
    if len(a) == 0:
        return "n=0"
    return f"n={len(a):3d}  win={(a>0).mean()*100:4.1f}%  exp={a.mean():+.3f}R  total={a.sum():+.1f}R"


def main():
    e = json.loads(ENTRIES.read_text())
    dates, states, gauge = build_regime()
    if dates is None:
        OUT.write_text("# Risk sentiment\n\nNo risk-gauge data available.\n", encoding="utf-8")
        print("no gauge"); return

    aligned, fighting, neutral = [], [], []
    for x in e:
        bet = risk_bet(x["symbol"], x["direction"])
        if bet == 0:
            neutral.append(x["r"]); continue
        reg = regime_at(dates, states, datetime.fromisoformat(x["time"]))
        if reg == 0:
            neutral.append(x["r"]); continue
        (aligned if (bet > 0) == (reg > 0) else fighting).append(x["r"])

    # Walk-forward on aligned vs fighting.
    es = sorted(e, key=lambda z: z["time"]); cut = int(len(es) * 0.7)

    def split_stats(sub):
        al, fi = [], []
        for x in sub:
            bet = risk_bet(x["symbol"], x["direction"])
            if bet == 0:
                continue
            reg = regime_at(dates, states, datetime.fromisoformat(x["time"]))
            if reg == 0:
                continue
            (al if (bet > 0) == (reg > 0) else fi).append(x["r"])
        return al, fi

    L = ["# Risk-sentiment (risk-on/off) filter test", "",
         f"Risk gauge: **{gauge}** daily trend (EMA20 vs EMA50). Each trade classified as a risk-on or "
         "risk-off bet via the exposure-factor model, then checked against the regime at its date.", "",
         "## Aligned with the risk regime vs fighting it", "",
         "| group | " + "stats |", "|---|---|",
         f"| ALIGNED (bet agrees with regime) | {stats(aligned)} |",
         f"| FIGHTING (bet against regime) | {stats(fighting)} |",
         f"| neutral (no risk tilt / flat regime) | {stats(neutral)} |"]

    ai, fi = split_stats(es[:cut]); ao, fo = split_stats(es[cut:])
    L += ["", "## Walk-forward (last 30% out-of-sample)", "",
          "| window | ALIGNED | FIGHTING |", "|---|---|---|",
          f"| in-sample | {stats(ai)} | {stats(fi)} |",
          f"| OUT-OF-SAMPLE | {stats(ao)} | {stats(fo)} |"]

    ea = np.mean(aligned) if aligned else 0
    ef = np.mean(fighting) if fighting else 0
    holds = (np.mean(ai) if ai else 0) > (np.mean(fi) if fi else 0) and \
            (np.mean(ao) if ao else 0) > (np.mean(fo) if fo else 0)
    L += ["", "## Verdict", "",
          f"- Aligned expectancy **{ea:+.3f}R** vs fighting **{ef:+.3f}R** (gap {ea-ef:+.3f}R).",
          "- " + ("Aligned beats fighting **in BOTH** in-sample and out-of-sample -> the risk regime "
                  "is a REAL, orthogonal filter; skipping/downweighting 'fighting' trades should help."
                  if holds else
                  "The edge does NOT hold in both windows -> not a reliable filter on this sample; "
                  "do NOT wire it. (Small samples once split by regime + time.)"),
          "", "_Coarse gauge (one index, EMA trend); USD trades excluded from the tilt; small "
          "per-cell samples — read directionally._"]
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"gauge={gauge}  aligned={stats(aligned)}  fighting={stats(fighting)}  holds_oos={holds}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
