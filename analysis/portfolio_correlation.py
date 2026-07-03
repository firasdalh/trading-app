"""Task 9 — validate the Risk Manager's factor-based correlation model against EMPIRICAL correlation.

The Risk Manager already refuses a 3rd correlated bet on one risk factor (app/risk/correlation.py).
This checks that its factor model is right: do symbols the model calls correlated (shared currency /
same bloc) actually move together? Fetches daily closes for the watchlist, computes the return
correlation matrix, and compares the average |corr| for model-correlated vs model-independent pairs.
Writes analysis/portfolio_correlation.md.

Fast: daily closes only (no per-bar technical). Run with uvicorn stopped:
    PYTHONPATH=backend python analysis/portfolio_correlation.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from sqlalchemy import select

from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass
from app.risk.correlation import exposure_factors

OUT = Path(__file__).with_name("portfolio_correlation.md")
BARS = 400


def _model_related(a: str, b: str) -> bool:
    """True if the factor model says these two LONGs share a risk factor (would net together)."""
    fa, fb = exposure_factors(a, "long"), exposure_factors(b, "long")
    return bool(set(fa) & set(fb))


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    items = s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()
    syms = [(it.symbol, it.asset_class) for it in items]
    s.close()

    rets = {}
    for sym, ac in syms:
        try:
            sd = get_broker_for(AssetClass(ac), bmap).get_ohlcv(sym, "1d", limit=BARS)
            closes = np.array([c.close for c in sd.candles], dtype=float) if sd and sd.candles else None
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: {exc}"); continue
        if closes is None or len(closes) < 30:
            continue
        rets[sym] = np.diff(np.log(closes))
    names = list(rets)
    m = min(len(rets[n]) for n in names)
    R = np.array([rets[n][-m:] for n in names])
    corr = np.corrcoef(R)

    lines = ["# Task 9 — Portfolio correlation (model vs empirical)", ""]
    lines.append(f"Daily-return correlation over ~{m} days for {len(names)} watchlist symbols. The "
                 "Risk Manager's factor model (app/risk/correlation.py) is validated against it.")
    lines += ["", "## Empirical correlation matrix (daily log returns)", ""]
    lines.append("| | " + " | ".join(n.replace("m", "").replace("M", "") for n in names) + " |")
    lines.append("|" + "---|" * (len(names) + 1))
    for i, ni in enumerate(names):
        cells = " | ".join(f"{corr[i, j]:+.2f}" for j in range(len(names)))
        lines.append(f"| **{ni.replace('m','').replace('M','')}** | {cells} |")

    related, indep = [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            (related if _model_related(names[i], names[j]) else indep).append(abs(corr[i, j]))
    ar = float(np.mean(related)) if related else 0.0
    ai = float(np.mean(indep)) if indep else 0.0

    # Strongest model-independent pairs (a blind spot if high).
    blind = sorted(((abs(corr[i, j]), names[i], names[j])
                    for i in range(len(names)) for j in range(i + 1, len(names))
                    if not _model_related(names[i], names[j])), reverse=True)[:5]

    lines += ["", "## Does the factor model capture real correlation?", "",
              f"- Mean |corr| for pairs the model calls **related**: **{ar:.2f}** "
              f"({len(related)} pairs).",
              f"- Mean |corr| for pairs the model calls **independent**: **{ai:.2f}** "
              f"({len(indep)} pairs).",
              f"- Separation: **{ar - ai:+.2f}** — "
              + ("the model's 'related' pairs are meaningfully more correlated than its 'independent' "
                 "ones, so the factor model is capturing the real structure." if ar - ai >= 0.1 else
                 "weak separation — the factor model may be missing cross-bloc correlation (see blind "
                 "spots below).")]
    lines += ["", "**Top model-'independent' pairs by empirical |corr| (potential blind spots):**"]
    for c, a, b in blind:
        lines.append(f"- {a} ~ {b}: |corr| {c:.2f}")

    lines += ["", "## What exists vs the Task-9 gap", "",
              "- **Exists (live):** `correlated_concentration` blocks a 3rd position that nets the same "
              "way on any factor (USD, JPY, equity bloc, metals, energy, crypto). Test "
              "`test_correlation_blocks_third_usd_bet` is exactly the Task-9 case. Effective per-factor "
              "cap ~= 2 x per-trade-risk (~6% at the 3% cap).",
              "- **Gap 1 (count vs risk-weighted):** the block counts POSITIONS, not summed risk — two "
              "0.5% positions and two 3% positions on USD are treated the same. A risk-amount-weighted "
              "per-factor cap would be tighter/fairer.",
              "- **Gap 2 (block vs resize):** it hard-blocks the 3rd bet rather than DOWNWEIGHTING it to "
              "fit a remaining factor budget (the total-exposure gate already resizes; the correlation "
              "gate does not).",
              "- **Gap 3 (cross-bloc):** the model treats blocs (equity / metals / energy) as separate; "
              "any high empirical corr between them above is unmodeled.", ""]
    lines.append("**Recommendation:** add a risk-weighted per-factor exposure cap (sum the % equity at "
                 "risk on each factor across open positions + the new trade; block OR resize to a "
                 "`max_correlated_exposure` budget). This is a LIVE-PATH Risk-Manager change — flagged "
                 "for explicit approval, implemented additively (strictly more conservative) and behind "
                 "a config value so it can't loosen any existing gate.")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} (related |corr| {ar:.2f} vs independent {ai:.2f})")


if __name__ == "__main__":
    main()
