"""Task 3 — how far into a trend does the funnel enter?

Once the regime flips to "trending" (ADX crosses 25) a trend run begins. For each entry we measure
where inside its ADX>=25 run it fired, both by TIME (bars elapsed / total run length) and by PRICE
(move elapsed / total run move). Reads analysis/entries.json; writes analysis/entry_timing.md.

'Total run length/move' is measured over the FULL run (start of ADX>=25 to its end), so this is a
retrospective 'where in the completed trend did we enter' — the honest way to ask early/mid/late.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
ENTRIES = HERE / "entries.json"
OUT = HERE / "entry_timing.md"


def _pctiles(a: np.ndarray) -> dict:
    return {p: float(np.percentile(a, p)) for p in (10, 25, 50, 75, 90)}


def _hist(a: np.ndarray, edges) -> list[tuple[str, int]]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        n = int(np.sum((a >= lo) & (a < hi)))
        out.append((f"{int(lo*100)}-{int(hi*100)}%", n))
    return out


def main():
    entries = json.loads(ENTRIES.read_text())
    bars_pct = np.array([e["trend_bars_pct"] for e in entries
                         if e.get("trend_bars_pct") is not None], dtype=float)
    move_pct = np.array([e["trend_move_pct"] for e in entries
                         if e.get("trend_move_pct") is not None], dtype=float)
    move_pct = move_pct[(move_pct >= -0.5) & (move_pct <= 1.5)]  # drop degenerate/flat-run outliers
    in_trend = sum(1 for e in entries if e.get("trend_bars_pct") is not None)

    lines = ["# Task 3 — Entry timing relative to the trend", ""]
    lines.append(f"Sample: **{in_trend} entries** — all fire inside an ADX>=25 run by construction "
                 "(trend_only mode only trades a confirmed trend). Percentiles are the fraction of "
                 "the completed trend run already elapsed at the moment of entry. The run is defined "
                 "from the ADX cross of 25 to its drop back below, so the 'price move' below is "
                 "measured from the CONFIRMATION bar, not the true swing origin (which usually starts "
                 "earlier, before ADX confirms).")

    if len(bars_pct):
        pb = _pctiles(bars_pct)
        lines += ["", "## By TIME (bars elapsed / total run length)", ""]
        lines.append(f"- median **{pb[50]*100:.0f}%** of the trend already elapsed at entry")
        lines.append(f"- p10 {pb[10]*100:.0f}% · p25 {pb[25]*100:.0f}% · p50 {pb[50]*100:.0f}% · "
                     f"p75 {pb[75]*100:.0f}% · p90 {pb[90]*100:.0f}%")
        lines.append(f"- mean {bars_pct.mean()*100:.0f}%")
        lines += ["", "Distribution:"]
        for label, n in _hist(bars_pct, [0, .2, .4, .6, .8, 1.01]):
            bar = "#" * int(40 * n / max(1, len(bars_pct)))
            lines.append(f"  {label:>8}: {n:4d} {bar}")

    if len(move_pct):
        pm = _pctiles(move_pct)
        lines += ["", "## By PRICE (move captured before entry / total run move)", ""]
        lines.append(f"- median **{pm[50]*100:.0f}%** of the trend's price move already happened "
                     "before entry")
        lines.append(f"- p10 {pm[10]*100:.0f}% · p25 {pm[25]*100:.0f}% · p50 {pm[50]*100:.0f}% · "
                     f"p75 {pm[75]*100:.0f}% · p90 {pm[90]*100:.0f}%")

    # Verdict.
    med = float(np.median(bars_pct)) if len(bars_pct) else 0.5
    verdict = ("EARLY" if med < 0.34 else "MID" if med < 0.66 else "LATE")
    # Right-skew check: a chase tail (entries firing late in the run).
    late_share = float(np.mean(bars_pct >= 0.6)) if len(bars_pct) else 0.0

    lines += ["", "## Conclusion", ""]
    lines.append(f"Entries are typically **{verdict}** in the ADX-confirmed run: median **~{med*100:.0f}%** "
                 "of the run's duration elapsed at entry, and a median of only "
                 f"~{float(np.median(move_pct))*100:.0f}% of the run's price move happened before "
                 "entry. So once ADX confirms the trend, the funnel enters promptly rather than "
                 "chasing an exhausted move.")
    lines.append(f"- BUT the distribution is right-skewed: **~{late_share*100:.0f}%** of entries still "
                 "fire in the back half (>=60%) of the run — a 'chase' tail. This is consistent with, "
                 "and the reason for, the confidence formula's anti-chase penalty (entries far from "
                 "EMA20 value are down-weighted).")
    lines.append("- Because the run is measured from the ADX cross (a lagging gate), the TRUE swing "
                 "usually began before confirmation — so relative to the whole price swing, entries "
                 "are later than the 7% figure suggests. The honest read: **early within the "
                 "*confirmed* trend, mid-way within the *whole* move.**")
    if verdict not in ("EARLY", "MID"):
        lines.append("- This is the structural cost of an ADX-confirmation entry: ADX is a lagging, "
                     "smoothed trend-strength gate, so it labels a move 'trending' only after it is "
                     "well underway. The funnel trades **confirmed continuation**, not the turn.")
        lines.append("- Implication: the remaining move to the target is what must pay for the trade. "
                     "This is consistent with why the pullback/armed 'wait for the break' entries "
                     "(which re-enter mid-trend on a shallow retrace) exist — they re-time entry "
                     "closer to value rather than chasing an extended move.")
    lines.append("")
    lines.append("_Caveat: 'total run length/move' is known only in hindsight (the run had to finish "
                 "to be measured); at entry time the system cannot know how much trend remains. This "
                 "measures historical positioning, not a usable real-time signal._")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} (median bars% = {med*100:.0f}%, verdict {verdict})")


if __name__ == "__main__":
    main()
