"""AI reviewer REPEATABILITY + significance over the last 30 days.

Addresses the right question: is the "AI 44% win" a stable edge or noise?
  1. Compute the ~93 last-30-day setups ONCE (deterministic, MT5 + technical), with each setup's
     realized R, confidence and ADX cached.
  2. Run the LIVE AI reviewer over the SAME setups N times, recording confirm/veto each pass.
  3. Report:
     - REPEATABILITY: veto-rate and confirmed-win% across runs (mean / min / max / std), and the
       per-setup FLIP rate (setups the AI confirms in some runs and vetoes in others = instability).
     - VETOED-TRADE win rate: of the ones it rejected, how many would have won (is it cutting losers
       or just cutting variance?).
     - SIGNIFICANCE: Wilson 95% CIs on deterministic vs AI win rates (do they even separate?).
     - CHEAP DETERMINISTIC FILTER: what confidence>=70% or ADX>=28 achieve on the SAME setups.

gpt-5-mini is a reasoning model (no temperature/seed), so run-to-run drift is expected — this
quantifies it. Uses OpenAI quota (N x #setups calls). Run with uvicorn stopped.
"""
from __future__ import annotations

import bisect
import statistics
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.agents.orchestrator import _deterministic_decision, run_orchestrator
from app.agents.pipeline import _timeframes_for
from app.agents.technical import run_technical
from app.backtest.simulator import _WINDOW, _neutral_fundamental, _simulate_trade
from app.brokers.registry import get_broker_for
from app.core.database import SessionLocal
from app.core.state import get_or_create_settings
from app.models.db import WatchItem
from app.models.enums import AssetClass, Direction
from app.models.schemas import OHLCVSeries

DAYS = 30
BARS = 1400
CONTEXT_BARS = 700
MAX_HOLD = 96
COOLDOWN = 3
COST_R = 0.05
N_RUNS = 5
OUT = Path(__file__).with_name("ai_repeatability.md")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _entry_ind(res, tf):
    prim = next((x for x in res.timeframes if x.timeframe == tf), res.timeframes[0])
    return prim.indicators or {}


def collect_setups(broker, symbol, ac, tf):
    tfs = _timeframes_for(tf)
    series = {}
    for t in tfs:
        limit = BARS if t == tf else CONTEXT_BARS
        try:
            sd = broker.get_ohlcv(symbol, t, limit=limit)
            series[t] = list(sd.candles) if sd and sd.candles else []
        except Exception:  # noqa: BLE001
            series[t] = []
    entry = series.get(tf, [])
    if len(entry) < _WINDOW + 5:
        return []
    ts_index = {t: [c.ts for c in series[t]] for t in tfs}
    fund = _neutral_fundamental(symbol)
    n = len(entry)
    cutoff = entry[-1].ts - timedelta(days=DAYS)
    out = []
    i = _WINDOW - 1
    while i < n:
        if entry[i].ts < cutoff:
            i += 1; continue
        t_i = entry[i].ts
        window, ok = [], True
        for t in tfs:
            w = (entry[max(0, i - _WINDOW + 1): i + 1] if t == tf
                 else series[t][max(0, bisect.bisect_right(ts_index[t], t_i) - _WINDOW):
                                bisect.bisect_right(ts_index[t], t_i)])
            if not w:
                ok = False; break
            window.append(OHLCVSeries(symbol=symbol, timeframe=t, candles=w))
        if not ok:
            i += 1; continue
        tech = run_technical(symbol, window, use_llm=False)
        prop = _deterministic_decision(symbol, ac, tf, tech, fund, now=t_i, trend_only=True)
        if not prop.is_actionable or prop.take_profit is None:
            i += 1; continue
        trade = _simulate_trade(symbol, entry, i, prop, max_hold=MAX_HOLD, cost_r=COST_R)
        if trade is None:
            i += 1; continue
        ind = _entry_ind(tech, tf)
        out.append({"symbol": symbol, "ac": ac, "tf": tf, "tech": tech, "ts": t_i,
                    "r": trade.r, "conf": prop.confidence, "adx": ind.get("adx") or 0.0})
        i = i + max(1, trade.bars_held) + COOLDOWN
    return out


def review_once(st):
    """Return 'confirm' / 'veto' / 'fail' for one setup this pass."""
    fund = _neutral_fundamental(st["symbol"])
    rev = run_orchestrator(st["symbol"], st["ac"], st["tf"], st["tech"], fund, now=st["ts"],
                           use_llm=True, trend_only=True, ai_led=False, scalp=False, st_band=False)
    if rev.review_decision == "veto":
        return "veto"
    if rev.review_decision == "confirm":
        return "confirm"
    return "fail"   # LLM didn't run / errored -> deterministic fallback (don't count as a real confirm)


def main():
    s = SessionLocal()
    bmap = get_or_create_settings(s).broker_map
    syms = [(it.symbol, it.asset_class, it.timeframe)
            for it in s.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all()]
    s.close()

    setups = []
    for sym, ac, tf in syms:
        try:
            setups += collect_setups(get_broker_for(AssetClass(ac), bmap), sym, AssetClass(ac), tf)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: collect FAILED {exc}")
    N = len(setups)
    print(f"collected {N} setups; running AI review x{N_RUNS}...")
    sys.stdout.flush()

    det_r = [st["r"] for st in setups]
    det_wins = sum(1 for r in det_r if r > 0)
    confirm_counts = [0] * N          # per-setup: how many runs confirmed it
    run_stats = []                    # per run: (veto_rate, conf_win%, vetoed_win%, fails)
    for run in range(N_RUNS):
        conf_r, veto_r, fails = [], [], 0
        for idx, st in enumerate(setups):
            v = review_once(st)
            if v == "confirm":
                confirm_counts[idx] += 1; conf_r.append(st["r"])
            elif v == "veto":
                veto_r.append(st["r"])
            else:
                fails += 1; conf_r.append(st["r"])  # fallback traded it
        cw = 100 * sum(1 for r in conf_r if r > 0) / len(conf_r) if conf_r else 0.0
        vw = 100 * sum(1 for r in veto_r if r > 0) / len(veto_r) if veto_r else 0.0
        vr = 100 * len(veto_r) / N if N else 0.0
        run_stats.append((vr, cw, vw, fails, len(conf_r), len(veto_r)))
        print(f"  run {run+1}: veto {len(veto_r)}/{N} ({vr:.0f}%)  conf-win {cw:.1f}%  "
              f"vetoed-win {vw:.1f}%  fails {fails}"); sys.stdout.flush()

    veto_rates = [x[0] for x in run_stats]
    conf_wins = [x[1] for x in run_stats]
    vetoed_wins = [x[2] for x in run_stats]
    flips = sum(1 for c in confirm_counts if 0 < c < N_RUNS)   # confirmed some runs, vetoed others

    det_lo, det_hi = wilson(det_wins, N)
    # AI-confirmed CI from the mean confirmed win rate and mean confirmed count across runs.
    mean_conf_n = statistics.mean(x[4] for x in run_stats)
    mean_conf_win = statistics.mean(conf_wins)
    ai_lo, ai_hi = wilson(round(mean_conf_win / 100 * mean_conf_n), round(mean_conf_n))

    def f70():
        sub = [st["r"] for st in setups if st["conf"] >= 0.70]
        return len(sub), (100 * sum(1 for r in sub if r > 0) / len(sub) if sub else 0.0)
    def fadx():
        sub = [st["r"] for st in setups if st["adx"] >= 28]
        return len(sub), (100 * sum(1 for r in sub if r > 0) / len(sub) if sub else 0.0)
    n70, w70 = f70(); nadx, wadx = fadx()

    L = ["# AI reviewer — repeatability & significance (last 30 days)", "",
         f"Same **{N} setups**, AI review run **{N_RUNS}x**. Deterministic baseline: "
         f"**{100*det_wins/N:.1f}%** win ({det_wins}/{N}).", "",
         "## 1. Repeatability (run-to-run on the SAME setups)", "",
         "| run | veto rate | confirmed win% | vetoed win% | LLM fails |", "|---|---|---|---|---|"]
    for i, (vr, cw, vw, fa, cn, vn) in enumerate(run_stats, 1):
        L.append(f"| {i} | {vr:.0f}% | {cw:.1f}% | {vw:.1f}% | {fa} |")
    L += ["",
          f"- **Veto rate:** mean {statistics.mean(veto_rates):.0f}%, range {min(veto_rates):.0f}-{max(veto_rates):.0f}%, "
          f"std {statistics.pstdev(veto_rates):.1f} pts.",
          f"- **Confirmed win%:** mean {statistics.mean(conf_wins):.1f}%, range {min(conf_wins):.1f}-{max(conf_wins):.1f}%, "
          f"std {statistics.pstdev(conf_wins):.1f} pts.",
          f"- **Unstable setups (confirmed in some runs, vetoed in others): {flips}/{N} "
          f"({100*flips/N:.0f}%)** — the AI flips its verdict on these. High = noise, not a stable filter.",
          "", "## 2. Is the AI cutting losers, or just variance?", "",
          f"- Win rate of the trades the AI VETOED: mean **{statistics.mean(vetoed_wins):.1f}%** across runs. "
          + ("Well below the confirmed win% -> it IS preferentially cutting losers."
             if statistics.mean(vetoed_wins) < statistics.mean(conf_wins) - 3 else
             "NOT much lower than the confirmed win% -> it's mostly cutting VARIANCE, not losers "
             "(which lowers total return without proving skill)."),
          "", "## 3. Statistical significance (Wilson 95% CI)", "",
          f"- Deterministic ({det_wins}/{N}): **{det_lo*100:.0f}%-{det_hi*100:.0f}%**",
          f"- AI-confirmed (~{mean_conf_win:.0f}% of ~{mean_conf_n:.0f}): **{ai_lo*100:.0f}%-{ai_hi*100:.0f}%**",
          "- " + ("The CIs OVERLAP heavily -> the win-rate 'improvement' is NOT statistically established "
                  "at this sample size. Treat as a first read." if ai_lo < det_hi else
                  "The CIs barely overlap -> a tentative separation, still small-sample."),
          "", "## 4. Cheaper deterministic filter on the SAME setups", "",
          f"- Confidence >= 70%: **{w70:.1f}%** win (n={n70})",
          f"- ADX >= 28: **{wadx:.1f}%** win (n={nadx})",
          f"- AI mean confirmed: **{statistics.mean(conf_wins):.1f}%** win (n~{mean_conf_n:.0f})",
          "- If a rule-based filter matches the AI's win% here, it's the better choice: deterministic, "
          "free, and zero repeatability problem.",
          "", "## Verdict", "",
          f"- Repeatability is the deciding factor: **{100*flips/N:.0f}% of setups flip** verdict between "
          "identical runs. gpt-5-mini is a reasoning model (no temperature/seed), so this drift is "
          "inherent — the current AI veto is **not a stable, reproducible filter**.",
          "- Options: (a) switch the reviewer to a NON-reasoning model at temperature 0 for "
          "determinism, (b) ensemble the review (vote over k calls) to average out the noise, or "
          "(c) prefer the deterministic confidence/ADX filter above if it matches the win%.",
          "", "_One 30-day sample, one machine-run; costs = flat 0.05R; favorable (trending) month "
          "so absolute win rates run high vs the long-run 34%._"]
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
