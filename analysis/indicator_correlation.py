"""Task 2 — pairwise correlation of the funnel's core signals AT ENTRY.

Are the gates (EMA slope, EMA alignment, ADX, MACD histogram, RSI) independent confirmations, or
mostly restating the same underlying momentum? Reads analysis/entries.json (from
generate_backtest_entries.py) and writes analysis/indicator_correlation.md.

Every feature is made DIRECTIONAL (multiplied by +1 for longs, -1 for shorts) and SCALE-FREE
(divided by ATR where it has price units), so a positive value always means "confirms the trade"
regardless of symbol or side — the only way the correlation question is meaningful.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
ENTRIES = HERE / "entries.json"
OUT = HERE / "indicator_correlation.md"

FEATURES = ["ema_align", "ema_slope", "adx", "macd_hist", "rsi_dir"]
LABELS = {
    "ema_align": "EMA20-50 alignment (/ATR)",
    "ema_slope": "EMA20 slope, 5-bar (/ATR)",
    "adx": "ADX (trend strength)",
    "macd_hist": "MACD histogram (/ATR)",
    "rsi_dir": "RSI vs 50 (directional)",
}


def _feature_row(e: dict):
    d = 1.0 if e.get("direction") == "long" else -1.0
    atr = e.get("atr")
    ema20, ema50 = e.get("ema20"), e.get("ema50")
    ema20p, macd, rsi, adx = e.get("ema20_prev5"), e.get("macd_hist"), e.get("rsi"), e.get("adx")
    if not atr or atr <= 0 or None in (ema20, ema50, ema20p, macd, rsi, adx):
        return None
    return [
        d * (ema20 - ema50) / atr,     # alignment/separation, direction-aligned
        d * (ema20 - ema20p) / atr,    # slope over 5 bars, direction-aligned
        adx,                            # strength (undirectional)
        d * macd / atr,                 # momentum, direction-aligned
        d * (rsi - 50.0),               # RSI momentum in the trade's direction
    ]


def _bar(r: float) -> str:
    a = abs(r)
    return "high" if a >= 0.6 else ("moderate" if a >= 0.3 else "low")


def main():
    entries = json.loads(ENTRIES.read_text())
    rows = [_feature_row(e) for e in entries]
    rows = [r for r in rows if r is not None]
    if len(rows) < 10:
        OUT.write_text(f"# Indicator correlation\n\nNot enough complete rows ({len(rows)}).\n", encoding="utf-8")
        print(f"only {len(rows)} rows"); return
    m = np.array(rows, dtype=float)
    corr = np.corrcoef(m, rowvar=False)

    lines = ["# Task 2 — Indicator correlation at entry", ""]
    lines.append(f"Sample: **{len(rows)} entries** (deterministic funnel, trend_only, all watchlist "
                 "symbols). Features are direction-aligned and ATR-normalized so a positive value "
                 "always means 'confirms the trade'.")
    lines += ["", "## Correlation matrix (Pearson r)", ""]
    header = "| | " + " | ".join(f.replace("_", " ") for f in FEATURES) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(FEATURES) + 1))
    for i, fi in enumerate(FEATURES):
        cells = " | ".join(f"{corr[i, j]:+.2f}" for j in range(len(FEATURES)))
        lines.append(f"| **{fi.replace('_', ' ')}** | {cells} |")

    # Pairwise summary, strongest first.
    pairs = []
    for i in range(len(FEATURES)):
        for j in range(i + 1, len(FEATURES)):
            pairs.append((abs(corr[i, j]), corr[i, j], FEATURES[i], FEATURES[j]))
    pairs.sort(reverse=True)

    lines += ["", "## Pairwise, strongest first", ""]
    for a, r, fi, fj in pairs:
        lines.append(f"- **{LABELS[fi]}** ~ **{LABELS[fj]}**: r = {r:+.2f} ({_bar(r)})")

    momentum = [p for p in pairs if {p[2], p[3]} <= {"ema_slope", "macd_hist", "rsi_dir"}]
    mom_high = [p for p in momentum if p[0] >= 0.6]
    adx_pairs = [p for p in pairs if "adx" in (p[2], p[3])]
    adx_indep = all(p[0] < 0.3 for p in adx_pairs)
    mean_abs = float(np.mean([p[0] for p in pairs]))

    lines += ["", "## Read", ""]
    if mom_high:
        lines.append("- The momentum-family signals (EMA slope, MACD histogram, RSI) are "
                     f"**{'strongly' if len(mom_high) >= 2 else 'partly'} correlated** — they are "
                     "largely **restating the same underlying momentum**, not independent votes. "
                     "Stacking them mostly compounds one signal; it does not add much orthogonal "
                     "confirmation.")
    else:
        lines.append("- The momentum-family signals (EMA slope, MACD, RSI) are only loosely related "
                     "here — they carry more independent information than a single momentum read.")
    lines.append(f"- **ADX** is {'largely INDEPENDENT' if adx_indep else 'somewhat related'} of the "
                 "directional momentum signals (as expected — it measures trend *strength*, not "
                 "*direction*), so it is the gate adding the most orthogonal information.")
    lines.append(f"- Mean |r| across all pairs = **{mean_abs:.2f}**. "
                 + ("Overall the gates are moderately-to-highly collinear — the funnel has fewer "
                    "truly independent confirmations than it has indicators."
                    if mean_abs >= 0.4 else
                    "Overall the gates are only weakly collinear — they carry reasonably distinct "
                    "information."))
    lines.append("")
    lines.append("_Caveat: correlations are at the point of entry only (a filtered, conditioned "
                 "sample — the funnel already required trend + momentum alignment to fire), so these "
                 "are conditional correlations, not unconditional indicator relationships._")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} rows, mean|r|={mean_abs:.2f})")


if __name__ == "__main__":
    main()
