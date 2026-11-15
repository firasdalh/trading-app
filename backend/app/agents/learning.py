"""A strategy that studies its own trades (asked 2026-11-12: "make the strategy learn from itself, so it keeps
getting better in the future").

Every couple of minutes (learning_tick), for every strategy with closed trades:

  1. FACTS - each closed trade of the strategy, as the journal books it (the broker's own history; the trades MT5
     tagged with the strategy's source), is kept once in strategy_trade_facts with
       - its result: the broker's $, and R where the app's own row holds the risk taken;
       - its FINGERPRINT (fingerprint()): what the market looked like at the entry - how many 1m closes in a row
         had already gone the trade's way, how far price stood from its average, the RSI, how busy the market
         was, the spread, the hour and session. The engine writes it into its 'opened' log as it opens a trade
         (since 2026-11-13); older trades get it from MT5's 1-minute history, read the same way.
       - its WHAT-IFS: the same entry replayed with other targets and stop sizes (WHATIF_TARGETS x WHATIF_STOPS)
         on the broker's own prices - the ticks of its first minute, then 1-minute candles (as the journal's
         signal review does: signal_review.simulate). A bigger stop means smaller lots, so each outcome is in R:
         -1 at the stop, the target's R at the target. Replayed once, a day after the entry.
  2. REVIEW (find_parts()) - the trades split by each part of the fingerprint. A part becomes a LESSON only when
     it is far from luck: at least MIN_TRADES trades in it and as many outside it, the same verdict in the older
     AND the newer half of them, and a Welch t of STRONG_T or more. Weaker ones show as "watching". The what-ifs
     give a lesson the same way when another target / stop beats the current one in both halves, by WHATIF_GAIN_R+.
  3. The board shows them ("What the strategy learned") with the change each one would make. NOTHING changes
     until the user presses Apply (strategy_routes: POST /{id}/learning/act): on a real account, a strategy that
     re-tunes itself on a few dozen trades chases luck - the 24 edits of 2026-11-11/12, by hand, did exactly that
     (study of the 221 trades, 2026-11-12). It never touches risk (RISK.md): the risk per trade, the cooldowns,
     the positions, the live switch.

Read-only on the broker: candles and ticks of the past (copy_rates_range / copy_ticks_range), never an order.
"""
from __future__ import annotations

import copy
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.sessions import SESSIONS, name_of as session_name, session_of
from app.models.db import AgentRun, StrategyConfig, StrategyTradeFact

log = logging.getLogger(__name__)

FP_VERSION = 1
MIN_TRADES = 25          # a part of the fingerprint is judged with at least this many trades in it and outside it
MIN_TOTAL = 60           # nothing is judged before the strategy has this many closed trades with an R
STRONG_T = 2.6           # |Welch t| for a lesson (~0.5% one-sided: across ~40 parts, about one review in five
                         # would show a false one by luck alone - the halves rule takes most of those out)
WATCH_T = 1.65           # |t| to be shown as "watching"
WHATIF_GAIN_R = 3.0      # another target / stop must beat the current one by this many R in all
WHATIF_TARGETS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.0)
WHATIF_STOPS = (0.75, 1.0, 1.5)
WHATIF_HORIZON_S = 24 * 3600
HISTORY_MIN = 300        # 1m bars read back for an old trade's fingerprint
MATCH_OPEN_S = 120       # the engine's 'opened' log and the broker's open time this near = the same trade
FILL_BUDGET = 40         # fingerprints / what-ifs filled from the broker's history per tick


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- #
#  The fingerprint
# --------------------------------------------------------------------------- #

def fingerprint(side: int, candles: list, entry: float, spread: float | None, at: datetime) -> dict | None:
    """What the market looked like when a ``side`` trade (1 buy, -1 sell) opened at ``entry`` at ``at``, from the
    1-minute candles CLOSED by then (oldest first; at least 60). Every number reads the trade's way: for a sell, a
    fall counts as "our way", RSI 30 reads as 70, and the bottom of the last hour as its top.
      streak     1m closes in a row that already went our way (each past the one before) - 0-1 = a fresh push
      ext20      how far the entry stands past the 1m EMA 20, in ATR
      run15      how far price already went our way in the last 15 minutes, in ATR
      rsi        RSI 14 on 1m, our side
      atr_rel    the 1m ATR against its own 50-bar average: under 1 = a quiet market
      pos60      where the entry sits in the last hour's range: 1 = at its top (its bottom for a sell)
      spread_pct the spread as % of the 1m ATR
      hour / session / weekday   on the UTC clock (app.core.sessions)
    None with too few candles or no ATR."""
    from app.agents.strategy import Bars

    if len(candles) < 60 or entry is None:
        return None
    b = Bars("1m", list(candles[-HISTORY_MIN:]))
    atr_s = b.atr(14)
    a = atr_s[-1] if atr_s else None
    if not a or a <= 0:
        return None
    closes = b.close
    streak = 0
    for j in range(len(closes) - 1, 0, -1):
        if side * (closes[j] - closes[j - 1]) > 0:
            streak += 1
        else:
            break
    ema20 = b.ema(20)[-1]
    rsi = b.rsi(14)[-1]
    tail = [x for x in atr_s[-50:] if x]
    hour_bars = b.c[-60:]
    hi, lo = max(c.high for c in hour_bars), min(c.low for c in hour_bars)
    at = _utc(at)
    return {
        "v": FP_VERSION,
        "streak": streak,
        "ext20": _r(side * (entry - ema20) / a, 3) if ema20 is not None else None,
        "run15": _r(side * (entry - closes[-15]) / a, 3),
        "rsi": _r(rsi if side > 0 else 100 - rsi, 1) if rsi is not None else None,
        "atr_rel": _r(a / (sum(tail) / len(tail)), 3) if tail else None,
        "pos60": _r(((entry - lo) if side > 0 else (hi - entry)) / (hi - lo), 3) if hi > lo else None,
        "spread_pct": _r(100 * spread / a, 1) if spread else None,
        "hour": at.hour,
        "session": session_of(at),
        "weekday": at.weekday(),
    }


def entry_fingerprint(broker, symbol: str, side: int, data: dict | None, entry: float, ctx) -> dict | None:
    """The fingerprint of a trade the engine is opening now: its own closed 1m bars when its rules read 1m, else
    1m read for it (strategy.load_bars). Never raises - a fingerprint must not stand in the way of a trade."""
    try:
        from app.agents.strategy import load_bars

        m1 = (data or {}).get("1m")
        if m1 is None or len(m1) < 60:
            m1 = load_bars(broker, symbol, {"1m"}, ctx.now).get("1m")
        if m1 is None:
            return None
        return fingerprint(side, m1.c, entry, getattr(ctx, "spread", None), ctx.now)
    except Exception as exc:  # noqa: BLE001
        log.info("learning: no fingerprint at entry", extra={"symbol": symbol, "error": str(exc)})
        return None


def history_fingerprint(broker, symbol: str, side: int, entry: float, at: datetime) -> dict | None:
    """The same fingerprint for a trade opened in the past, from MT5's history: the 1m candles that closed by
    ``at``, and the spread from the ticks around it."""
    from app.agents.signal_review import _bars, _ticks

    at = _utc(at)
    minute = at.replace(second=0, microsecond=0)
    candles = _bars(broker, symbol, "1m", minute - timedelta(minutes=HISTORY_MIN), minute)
    ticks = _ticks(broker, symbol, at - timedelta(seconds=30), at + timedelta(seconds=30))
    spreads = sorted(a - b for _t, b, a in ticks)
    spread = spreads[len(spreads) // 2] if spreads else None
    return fingerprint(side, candles, entry, spread, at)


# --------------------------------------------------------------------------- #
#  The what-ifs
# --------------------------------------------------------------------------- #

def whatif_key(target: float, stop: float) -> str:
    return f"t{target:g}|s{stop:g}"


def whatifs(side: int, entry: float, risk: float, path: list, horizon_end: datetime, now: datetime,
            at: datetime | None = None) -> dict | None:
    """Every target / stop of the grid replayed on ``path`` (signal_review.simulate's (time, high, low, close,
    spread) of bid prices from the entry on): {whatif_key: [R, seconds held]}. A stop k times the real one with the
    lots 1/k keeps the $ at risk, so each outcome is -1 at its stop or the target's R. How long it would have been
    held matters as much (2026-11-13, dry run): a wider stop and a further target won per trade, but the trade then
    blocks its pair for hours and the next entries never happen - whatif_table replays them in order. None while
    one can still change."""
    from app.agents.signal_review import simulate

    if not risk or risk <= 0:
        return None
    at = _utc(at) if at is not None else (path[0][0] if path else None)
    out: dict[str, list] = {}
    for k in WHATIF_STOPS:
        d = risk * k
        for t in WHATIF_TARGETS:
            res = simulate(side, entry, entry - side * d, entry + side * t * d, path, horizon_end, now)
            if res.get("result") not in ("win", "loss", "timeout") or res.get("r") is None:
                return None
            held = None
            if res.get("exit_at") and at is not None:
                held = max(0, int((datetime.fromisoformat(res["exit_at"]) - at).total_seconds()))
            out[whatif_key(t, k)] = [round(float(res["r"]), 3), held]
    return out


def _wr(v) -> tuple[float, int | None]:
    """A what-if value: [R, seconds held] (a bare R held for an unknown time)."""
    if isinstance(v, (list, tuple)):
        return float(v[0]), (int(v[1]) if len(v) > 1 and v[1] is not None else None)
    return float(v or 0.0), None


def history_whatifs(broker, symbol: str, side: int, entry: float, risk: float, at: datetime,
                    now: datetime) -> dict | None:
    """The what-ifs of a past entry, from the broker's prices: the ticks of its first minute, then 1m candles, up
    to WHATIF_HORIZON_S after it (the bid; a sell adds the first minute's spread)."""
    from app.agents.signal_review import _bars, _ticks

    at = _utc(at)
    first_min_end = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    ticks = _ticks(broker, symbol, at, first_min_end)
    spreads = sorted(a - b for _t, b, a in ticks)
    spread = spreads[len(spreads) // 2] if spreads else 0.0
    horizon_end = at + timedelta(seconds=WHATIF_HORIZON_S)
    path = [(t, b, b, b, a - b) for t, b, a in ticks]
    for c in _bars(broker, symbol, "1m", first_min_end, min(now, horizon_end) + timedelta(minutes=1)):
        path.append((_utc(c.ts), c.high, c.low, c.close, spread))
    return whatifs(side, entry, risk, path, horizon_end, now, at=at)


# --------------------------------------------------------------------------- #
#  The parts of a fingerprint the review splits trades by
# --------------------------------------------------------------------------- #

def _cut(v, edges: tuple[float, float]) -> int | None:
    if v is None:
        return None
    return 0 if v < edges[0] else 1 if v < edges[1] else 2


def _three(field: str, edges: tuple[float, float], labels: tuple[str, str, str]):
    def part(f: dict):
        i = _cut((f.get("fp") or {}).get(field), edges)
        return None if i is None else (("low", "mid", "high")[i], labels[i])
    return part


def _streak(f: dict):
    v = (f.get("fp") or {}).get("streak")
    if v is None:
        return None
    if v <= 1:
        return "0-1", "a fresh push: at most 1 close our way before the entry"
    if v <= 3:
        return "2-3", "2-3 1m closes in a row already our way before the entry"
    return "4+", "4+ 1m closes in a row already our way before the entry (chasing)"


def _fp_field(field: str, label):
    def part(f: dict):
        v = (f.get("fp") or {}).get(field)
        return None if v is None else (str(v), label(v))
    return part


def _hour_label(h) -> str:
    return f"opened {int(h):02d}:00-{(int(h) + 1) % 24:02d}:00 UTC"


# key -> (what the board calls it, how a trade is placed in it)
FEATURES: dict[str, tuple[str, object]] = {
    "streak": ("1m closes already our way", _streak),
    "run15": ("the last 15 minutes", _three("run15", (1.5, 3.5), (
        "price had moved under 1.5 ATR our way in the last 15 min", "price had moved 1.5-3.5 ATR our way in the "
        "last 15 min", "price had already run 3.5+ ATR our way in the last 15 min"))),
    "ext20": ("distance from the 1m EMA 20", _three("ext20", (1.2, 2.2), (
        "the entry within 1.2 ATR of the 1m EMA 20", "the entry 1.2-2.2 ATR past the 1m EMA 20",
        "the entry stretched 2.2+ ATR past the 1m EMA 20"))),
    "rsi": ("RSI 1m", _three("rsi", (60, 70), (
        "RSI 1m under 60 (our side)", "RSI 1m 60-70 (our side)", "RSI 1m 70+ (our side): stretched"))),
    "atr_rel": ("how busy the market is", _three("atr_rel", (0.9, 1.15), (
        "a quiet market: 1m ATR under 0.9x its average", "a normal market: 1m ATR 0.9-1.15x its average",
        "a busy market: 1m ATR 1.15x+ its average"))),
    "pos60": ("place in the last hour's range", _three("pos60", (0.8, 0.95), (
        "the entry in the lower 80% of the last hour's range (our side)", "the entry in the top 80-95% of the "
        "last hour's range (our side)", "the entry at the very top of the last hour's range (bottom for a "
        "sell)"))),
    "spread": ("spread", _three("spread_pct", (10, 20), (
        "spread under 10% of the 1m ATR", "spread 10-20% of the 1m ATR", "spread 20%+ of the 1m ATR"))),
    "pair": ("pair", lambda f: (f["symbol"], f["symbol"])),
    "side": ("side", lambda f: (f["side"], "buys" if f["side"] == "long" else "sells")),
    "session": ("session", _fp_field("session", lambda k: f"the {session_name(k)} session")),
    "hour": ("hour (UTC)", _fp_field("hour", _hour_label)),
}


# --------------------------------------------------------------------------- #
#  What a lesson would change (applied by strategy_routes, only on the user's click)
# --------------------------------------------------------------------------- #

def _session_hours(key: str) -> list[int]:
    s = next((x for x in SESSIONS if x["key"] == key), None)
    return list(range(s["start"], s["end"])) if s else []


def allowed_hours(hours: dict | None) -> set[int] | None:
    """The UTC hours a strategy may open in, as hours: None when it keeps a clock window (start / end on a zone),
    which no hour can be taken out of here."""
    if not hours:
        return set(range(24))
    if hours.get("hours_utc"):
        return {int(h) for h in hours["hours_utc"]}
    if hours.get("sessions"):
        return {h for k in hours["sessions"] for h in _session_hours(k)}
    return None


def _on_entry_groups(cfg: dict) -> list[int]:
    off = set((cfg.get("off_groups") or {}).get("entry_long") or [])
    return [i for i, g in enumerate(cfg.get("entry_long") or []) if g and i not in off]


def _chase_cap(cfg: dict) -> int | None:
    """The loosest 'dont_chase' cap across the BUY groups that trade (None when one has none)."""
    caps = []
    for i in _on_entry_groups(cfg):
        mine = [int((c.get("params") or {}).get("max", 1)) for c in cfg["entry_long"][i] if c.get("type") == "dont_chase"]
        if not mine:
            return None
        caps.append(min(mine))
    return max(caps) if caps else None


def _rule_value(cfg: dict, type_: str, key: str, tf: str = "1m") -> list[float]:
    """``key`` of every ``type_`` condition on ``tf`` in the BUY groups that trade."""
    out = []
    for i in _on_entry_groups(cfg):
        for c in cfg["entry_long"][i]:
            if c.get("type") == type_ and c.get("tf") == tf:
                out.append(float((c.get("params") or {}).get(key, 0)))
    return out


def action_for(lesson: dict, cfg: dict) -> dict | None:
    """The change a lesson asks for, on a strategy as plain data (strategy_routes._config_of): {"op", ..., "text",
    "done"} - ``done`` when the strategy already does it. None when no setting on the board says it yet."""
    feat, bucket, kind = lesson["feature"], lesson["bucket"], lesson["kind"]
    if kind == "whatif":
        t, k = lesson["target"], lesson["stop"]
        stop_to = round(min(10.0, max(0.3, float(cfg.get("stop_atr") or 1.5) * k)), 2)
        parts = []
        if t != cfg.get("target_r"):
            parts.append(f"target {cfg.get('target_r'):g}R → {t:g}R")
        if k != 1.0:
            parts.append(f"stop {float(cfg.get('stop_atr') or 0):g}× ATR → {stop_to:g}× ATR")
        return {"op": "levels", "target": t, "stop_atr": stop_to,
                "text": ("Set " + " and ".join(parts)) if parts else "Already set", "done": not parts}
    weak = kind == "weak"
    if feat == "pair" and weak:
        pairs = [p.get("symbol") for p in cfg.get("pairs") or []]
        return {"op": "remove_pair", "symbol": bucket, "text": f"Stop trading {bucket}", "done": bucket not in pairs}
    if feat == "side" and weak:
        other = "short" if bucket == "long" else "long"
        if cfg.get("sides") not in ("both", other):
            return None
        return {"op": "sides", "to": other, "text": f"Trade {'sells' if other == 'short' else 'buys'} only",
                "done": cfg.get("sides") == other}
    if feat in ("session", "hour") and weak:
        hrs = _session_hours(bucket) if feat == "session" else [int(bucket)]
        now_on = allowed_hours(cfg.get("hours"))
        if now_on is None:
            return None
        if not now_on - set(hrs):
            return None
        what = f"the {session_name(bucket)} session" if feat == "session" else _hour_label(bucket)[7:]
        return {"op": "remove_hours", "hours": hrs, "text": f"Stop opening trades in {what}",
                "done": not (now_on & set(hrs))}
    if feat == "streak" and (weak or bucket == "0-1"):
        if not _on_entry_groups(cfg):
            return None
        cap = 3 if (weak and bucket == "4+") else 1
        have = _chase_cap(cfg)
        return {"op": "dont_chase", "max": cap,
                "text": f"Add “Not chasing: at most {cap} close{'s' if cap != 1 else ''} in a row our way · 1m” "
                        "to the BUY rules (sells read it mirrored)",
                "done": have is not None and have <= cap}
    if feat == "spread" and weak and bucket in ("mid", "high"):
        if not _on_entry_groups(cfg):
            return None
        cap = 10 if bucket == "mid" else 20
        have = _rule_value(cfg, "spread", "max_pct")
        return {"op": "spread_max", "pct": cap, "text": f"Only enter while the spread is at most {cap}% of the 1m ATR",
                "done": bool(have) and max(have) <= cap}
    if feat == "atr_rel" and weak and bucket == "low":
        if not _on_entry_groups(cfg):
            return None
        have = _rule_value(cfg, "atr", "mult")
        return {"op": "atr_min", "mult": 0.9, "text": "Only enter while the 1m ATR is at least 0.9x its 50-bar average",
                "done": bool(have) and min(have) >= 0.9}
    return None


def apply_change(cfg: dict, op: dict) -> dict:
    """``cfg`` (a strategy as plain data) with the change ``op`` made - a new dict; the caller validates it
    (StrategyIn) before anything is saved. Raises ValueError when it can't be made."""
    from app.agents.strategy import _MAX_CONDITIONS

    out = copy.deepcopy(cfg)
    kind = op["op"]
    if kind == "remove_pair":
        keep = [p for p in out.get("pairs") or [] if p.get("symbol") != op["symbol"]]
        if not keep:
            raise ValueError("it would leave the strategy no pair to trade")
        out["pairs"] = keep
    elif kind == "sides":
        out["sides"] = op["to"]
    elif kind == "remove_hours":
        now_on = allowed_hours(out.get("hours"))
        if now_on is None:
            raise ValueError("its hours are a clock window - change them on the board")
        left = sorted(now_on - set(op["hours"]))
        if not left:
            raise ValueError("it would leave the strategy no hour to open trades in")
        h = out.get("hours") or {}
        out["hours"] = {"hours_utc": left, "tz": h.get("tz"), **({"crypto_24h": True} if h.get("crypto_24h") else {})}
    elif kind in ("dont_chase", "spread_max", "atr_min"):
        type_, params = {"dont_chase": ("dont_chase", {"max": op.get("max", 1)}),
                         "spread_max": ("spread", {"max_pct": op.get("pct", 20)}),
                         "atr_min": ("atr", {"period": 14, "mult": op.get("mult", 0.9), "avg": 50, "bars": 1})}[kind]
        groups = _on_entry_groups(out)
        if not groups:
            raise ValueError("no BUY group is on to add it to")
        for i in groups:
            g = out["entry_long"][i]
            mine = [c for c in g if c.get("type") == type_ and c.get("tf") == "1m"]
            if mine:
                for c in mine:
                    c["params"] = {**(c.get("params") or {}), **params}
            elif len(g) >= _MAX_CONDITIONS:
                raise ValueError(f"BUY group {i + 1} already has {_MAX_CONDITIONS} conditions")
            else:
                g.append({"type": type_, "tf": "1m", "params": dict(params)})
    elif kind == "levels":
        out["target_r"] = op["target"]
        out["stop_atr"] = op["stop_atr"]
    else:
        raise ValueError(f"unknown change '{kind}'")
    return out


# --------------------------------------------------------------------------- #
#  The review
# --------------------------------------------------------------------------- #

def _stats(rs: list[float]) -> tuple[float, float]:
    n = len(rs)
    m = sum(rs) / n
    v = sum((x - m) ** 2 for x in rs) / (n - 1) if n > 1 else 0.0
    return m, v


def welch_t(a: list[float], b: list[float]) -> float:
    """(mean a - mean b) over its standard error; 0 when either side has under 2 trades or no spread at all."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, va = _stats(a)
    mb, vb = _stats(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se > 0 else 0.0


def _summary(fs: list[dict]) -> dict:
    rs = [f["r"] for f in fs]
    n = len(rs)
    return {"n": n, "won": round(sum(1 for x in rs if x > 0) / n, 3) if n else None,
            "r_sum": round(sum(rs), 2), "avg_r": round(sum(rs) / n, 3) if n else None,
            "pnl": round(sum(f.get("pnl") or 0.0 for f in fs), 2)}


def _halves(fs: list[dict]) -> list[float]:
    """R in the older and the newer half of a part's trades (by open time)."""
    h = len(fs) // 2
    return [round(sum(f["r"] for f in fs[:h]), 2), round(sum(f["r"] for f in fs[h:]), 2)]


def find_parts(facts: list[dict]) -> list[dict]:
    """Every part of the fingerprint with enough trades in it and outside it, with its numbers and its verdict:
    kind "weak" (clearly worse than the rest and losing, in both halves), "sweet" (clearly better and winning in
    both halves), else None; strength "strong" (|t| >= STRONG_T) or "watching"."""
    facts = sorted([f for f in facts if f.get("r") is not None], key=lambda f: f["opened_at"])
    out = []
    for feat, (fname, place) in FEATURES.items():
        groups: dict[str, list[dict]] = {}
        labels: dict[str, str] = {}
        for f in facts:
            p = place(f)
            if p is None:
                continue
            groups.setdefault(p[0], []).append(f)
            labels[p[0]] = p[1]
        counted = sum(len(v) for v in groups.values())
        for bucket, fs in groups.items():
            rest = [f for k, v in groups.items() if k != bucket for f in v]
            if len(fs) < MIN_TRADES or len(rest) < MIN_TRADES:
                continue
            t = welch_t([f["r"] for f in fs], [f["r"] for f in rest])
            halves = _halves(fs)
            s = _summary(fs)
            kind = strength = None
            if t <= -WATCH_T and s["r_sum"] < 0 and all(h < 0 for h in halves):
                kind = "weak"
            elif t >= WATCH_T and s["r_sum"] > 0 and all(h > 0 for h in halves):
                kind = "sweet"
            if kind:
                strength = "strong" if abs(t) >= STRONG_T else "watching"
            out.append({"key": f"{feat}:{bucket}", "feature": feat, "feature_name": fname, "bucket": bucket,
                        "label": labels[bucket], "kind": kind, "strength": strength, "t": round(t, 2),
                        **s, "halves": halves, "rest": _summary(rest), "counted": counted})
    return out


def sequenced(fs: list[dict], key: str, rules: tuple[int, int, int]) -> list[tuple[dict, float, int | None]]:
    """The entries ``fs`` (oldest first) as the strategy could have taken them with what-if ``key``'s exits: one
    trade per pair, ``rules`` = (minutes to wait after a close on the pair, minutes after a loss on the same side,
    most trades open at once - 0 = no cap), each trade holding its pair until the what-if's exit. A setting that
    holds longer loses the entries that came while it was still in (the 3m test of 2026-11-12: the same edge per
    trade, half the trades). Entries the real trades blocked can't be added back, so a setting holding SHORTER
    than the real trades did is, if anything, under-counted."""
    cooldown, loss_cooldown, cap = rules
    busy: dict[str, dict] = {}
    held: list[float] = []
    out = []
    for f in fs:
        o = f["opened_at"].timestamp()
        held = [h for h in held if h > o]
        b = busy.get(f["symbol"])
        if b and (o < b["end"] + 60 * cooldown or (b["loss"] and b["side"] == f["side"]
                                                     and o < b["end"] + 60 * loss_cooldown)):
            continue
        if cap and len(held) >= cap:
            continue
        r, secs = _wr((f["whatif"] or {}).get(key))
        end = o + (secs or 0)
        busy[f["symbol"]] = {"end": end, "loss": r < 0, "side": f["side"]}
        held.append(end)
        out.append((f, r, secs))
    return out


def whatif_table(facts: list[dict], target_r: float | None, rules: tuple[int, int, int] = (0, 5, 0)) -> dict | None:
    """The grid of targets x stop sizes: each cell's R over the trades the strategy could have taken with it
    (sequenced(): a longer hold loses the entries that came meanwhile), with its trade count, win rate and median
    hold; the current setting marked; and the best other cell with how sure its gain is - in total, in the older
    and the newer half (each replayed on its own), and per trade (a paired t over every entry, unsequenced)."""
    fs = sorted([f for f in facts if f.get("whatif")], key=lambda f: f["opened_at"])
    if not fs:
        return None
    keys = [whatif_key(t, k) for k in WHATIF_STOPS for t in WHATIF_TARGETS]
    h = len(fs) // 2
    halves = (fs[:h], fs[h:])

    def cell(key: str) -> dict:
        taken = sequenced(fs, key, rules)
        rs = [r for _f, r, _s in taken]
        holds = sorted(s for _f, _r, s in taken if s is not None)
        return {"r": round(sum(rs), 2), "n": len(rs), "won": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else None,
                "hold_min": round(holds[len(holds) // 2] / 60) if holds else None}

    cells = {k: cell(k) for k in keys}
    cur = None
    if target_r:
        t_near = min(WHATIF_TARGETS, key=lambda t: abs(t - float(target_r)))
        if abs(t_near - float(target_r)) < 0.051:
            cur = whatif_key(t_near, 1.0)
    best = max(keys, key=lambda k: cells[k]["r"])
    out = {"n": len(fs), "targets": list(WHATIF_TARGETS), "stops": list(WHATIF_STOPS), "cells": cells,
           "current": cur, "best": best, "actual_r": round(sum(f["r"] for f in fs if f.get("r") is not None), 2),
           "rules": {"cooldown_min": rules[0], "loss_cooldown_min": rules[1], "max_open": rules[2]}, "gain": None}
    if cur and best != cur:
        d = [_wr((f["whatif"] or {}).get(best))[0] - _wr((f["whatif"] or {}).get(cur))[0] for f in fs]
        m, v = _stats(d) if len(d) > 1 else (0.0, 0.0)
        t = m / math.sqrt(v / len(d)) if v > 0 else 0.0
        gh = [round(sum(r for _f, r, _s in sequenced(part, best, rules))
                    - sum(r for _f, r, _s in sequenced(part, cur, rules)), 2) for part in halves]
        out["gain"] = {"r": round(cells[best]["r"] - cells[cur]["r"], 2), "t": round(t, 2), "halves": gh}
    return out


def _whatif_lesson(table: dict | None) -> dict | None:
    if not table or not table.get("gain") or table["n"] < MIN_TOTAL:
        return None
    g = table["gain"]
    if g["r"] < WHATIF_GAIN_R or not all(x > 0 for x in g["halves"]) or g["t"] < WATCH_T:
        return None
    t, k = table["best"].split("|")
    target, stop = float(t[1:]), float(k[1:])
    cur_t = float(table["current"].split("|")[0][1:])
    best, cur = table["cells"][table["best"]], table["cells"][table["current"]]
    return {"key": f"whatif:{table['best']}", "feature": "whatif", "feature_name": "target and stop",
            "bucket": table["best"], "kind": "whatif", "strength": "strong" if g["t"] >= STRONG_T else "watching",
            "t": g["t"], "target": target, "stop": stop, "n": best["n"], "won": best["won"], "r_sum": best["r"],
            "label": f"target {target:g}R" + (f" with a stop {stop:g}× as wide" if stop != 1.0 else "")
                     + f" instead of {cur_t:g}R: {best['r']:+.1f}R over {best['n']} trades against {cur['r']:+.1f}R "
                       f"over {cur['n']}",
            "halves": g["halves"], "gain_r": g["r"]}


def title_of(p: dict) -> str:
    if p["kind"] == "weak":
        return f"Losing: {p['label']}"
    if p["kind"] == "sweet":
        return f"Winning: {p['label']}"
    return p["label"]


# --------------------------------------------------------------------------- #
#  Facts: the strategy's closed trades, kept once each
# --------------------------------------------------------------------------- #

def fact_key(sid: int, symbol: str, direction: str, opened_at: datetime) -> str:
    from app.risk.service import _norm_symbol

    return f"{sid}|{_norm_symbol(symbol)}|{direction}|{int(_utc(opened_at).timestamp())}"


def _fact_view(row: StrategyTradeFact) -> dict:
    return {"id": row.id, "symbol": row.symbol, "side": row.side, "opened_at": _utc(row.opened_at),
            "closed_at": _utc(row.closed_at), "pnl": row.pnl, "r": row.r, "fp": row.fp, "whatif": row.whatif}


def facts_of(session: Session, sid: int) -> list[dict]:
    rows = session.scalars(select(StrategyTradeFact).where(StrategyTradeFact.strategy_id == sid)
                           .order_by(StrategyTradeFact.opened_at)).all()
    return [_fact_view(r) for r in rows]


def _opened_logs(session: Session, sid: int, since: datetime) -> list[AgentRun]:
    return list(session.scalars(
        select(AgentRun).where(AgentRun.agent == "strategy", AgentRun.event == "opened",
                               func.json_extract(AgentRun.detail, "$.strategy_id") == sid,
                               AgentRun.created_at >= since.replace(tzinfo=None))
        .order_by(AgentRun.id)).all())


def sync_facts(session: Session, cfg: StrategyConfig, trades: list) -> int:
    """Keep each of the strategy's closed trades (``trades``: (PositionView, the app's row or None) from the
    journal's book) once, with its result, and the entry, stop and fingerprint of the engine's 'opened' log when
    one matches it. Results are re-read every time (a later true-up or Repair corrects them). Returns how many
    were new."""
    from app.models.db import entry_risk
    from app.risk.service import _norm_symbol

    have = {r.key: r for r in session.scalars(select(StrategyTradeFact)
                                              .where(StrategyTradeFact.strategy_id == cfg.id)).all()}
    todo = []
    for v, row in trades:
        if v.realized_pnl is None or v.opened_at is None:
            continue
        risk = entry_risk(row) if row is not None else None
        r = round(v.realized_pnl / risk, 3) if risk else None
        key = fact_key(cfg.id, v.symbol, str(v.direction), v.opened_at)
        hit = have.get(key)
        if hit is not None:
            if hit.pnl != round(v.realized_pnl, 2) or (r is not None and hit.r != r):
                hit.pnl = round(v.realized_pnl, 2)
                if r is not None:
                    hit.r = r
            continue
        if key in {k for k, _v, _r in todo}:
            continue
        todo.append((key, v, r))
    if not todo:
        return 0
    logs = _opened_logs(session, cfg.id, min(_utc(v.opened_at) for _k, v, _r in todo) - timedelta(days=1))
    used: set[int] = set()
    for key, v, r in todo:
        o = _utc(v.opened_at)
        side = str(v.direction)
        near = [x for x in logs if x.id not in used and _norm_symbol(x.symbol or "") == _norm_symbol(v.symbol)
                and (x.detail or {}).get("side") == side
                and abs((_utc(x.created_at) - o).total_seconds()) <= MATCH_OPEN_S]
        op = min(near, key=lambda x: abs((_utc(x.created_at) - o).total_seconds()), default=None)
        d = (op.detail or {}) if op is not None else {}
        if op is not None:
            used.add(op.id)
        entry, stop = d.get("entry"), d.get("stop")
        fp = d.get("fp") if isinstance(d.get("fp"), dict) else None
        session.add(StrategyTradeFact(
            key=key, strategy_id=cfg.id, symbol=v.symbol, side=side, opened_at=o, closed_at=_utc(v.closed_at),
            pnl=round(v.realized_pnl, 2), r=r, entry=float(v.entry_price) if v.entry_price else entry,
            risk_dist=abs(float(entry) - float(stop)) if entry is not None and stop is not None else None,
            fp=fp, fp_from="entry" if fp else None))
    session.flush()
    return len(todo)


def fill_from_history(session: Session, cfg: StrategyConfig, broker_for, now: datetime,
                      budget: int = FILL_BUDGET) -> int:
    """Give up to ``budget`` facts what they still miss, from the broker's history: a fingerprint (older trades,
    opened before the engine wrote one), and the what-ifs once their day is over. Returns the work done."""
    classes = {p.get("symbol"): p.get("asset_class") for p in cfg.pairs or []}
    ripe = (now - timedelta(seconds=WHATIF_HORIZON_S)).replace(tzinfo=None)
    need_fp = StrategyTradeFact.fp.is_(None) & (func.coalesce(StrategyTradeFact.fp_tries, 0) < 3)
    # What-ifs once the replay window is over (a day after the entry): one read each, final at once.
    need_wi = (StrategyTradeFact.whatif.is_(None) & StrategyTradeFact.risk_dist.is_not(None)
               & (StrategyTradeFact.opened_at <= ripe) & (func.coalesce(StrategyTradeFact.whatif_tries, 0) < 3))
    rows = session.scalars(select(StrategyTradeFact)
                           .where(StrategyTradeFact.strategy_id == cfg.id, need_fp | need_wi)
                           .order_by(StrategyTradeFact.opened_at.desc())).all()
    done = 0
    for row in rows:
        if done >= budget:
            break
        side = 1 if row.side == "long" else -1
        try:
            broker = broker_for(classes.get(row.symbol) or "forex")
        except Exception:  # noqa: BLE001
            continue
        o = _utc(row.opened_at)
        if row.fp is None and row.entry and (row.fp_tries or 0) < 3:
            done += 1
            try:
                fp = history_fingerprint(broker, row.symbol, side, float(row.entry), o)
            except Exception as exc:  # noqa: BLE001
                log.info("learning: history fingerprint failed", extra={"symbol": row.symbol, "error": str(exc)})
                fp = None
            if fp:
                row.fp, row.fp_from = fp, "history"
            else:
                row.fp_tries = (row.fp_tries or 0) + 1
        if row.whatif is None and row.risk_dist and row.entry and (row.whatif_tries or 0) < 3:
            if now < o + timedelta(seconds=WHATIF_HORIZON_S):
                continue
            done += 1
            try:
                w = history_whatifs(broker, row.symbol, side, float(row.entry), float(row.risk_dist), o, now)
            except Exception as exc:  # noqa: BLE001
                log.info("learning: what-ifs failed", extra={"symbol": row.symbol, "error": str(exc)})
                w = None
            if w:
                row.whatif = w
            else:
                row.whatif_tries = (row.whatif_tries or 0) + 1
    session.flush()
    return done


# --------------------------------------------------------------------------- #
#  The view the board shows
# --------------------------------------------------------------------------- #

def _events(session: Session, sid: int) -> list[AgentRun]:
    return list(session.scalars(
        select(AgentRun).where(AgentRun.agent == "learning",
                               func.json_extract(AgentRun.detail, "$.strategy_id") == sid)
        .order_by(AgentRun.id)).all())


def decisions(session: Session, sid: int) -> dict:
    """Per lesson key: its last decision ("applied" / "ignored"; gone after an undo or un-ignore) with when and
    what it changed; and when each strong lesson was first seen."""
    state: dict[str, dict] = {}
    seen: dict[str, str] = {}
    for e in _events(session, sid):
        d = e.detail or {}
        key = d.get("key")
        if not key:
            continue
        at = _utc(e.created_at).isoformat()
        if e.event == "found":
            seen.setdefault(key, at)
        elif e.event in ("applied", "ignored"):
            state[key] = {"status": e.event, "at": at, "id": e.id, "title": d.get("title"),
                          "changes": d.get("changes"), "before": d.get("before"), "after": d.get("after"),
                          "stats": d.get("stats")}
        elif e.event in ("undone", "unignored"):
            state.pop(key, None)
    return {"state": state, "seen": seen}


def _version(session: Session, sid: int, facts: list[dict]) -> dict:
    last = session.scalars(select(AgentRun).where(AgentRun.agent == "strategy", AgentRun.event == "edited",
                                                  func.json_extract(AgentRun.detail, "$.strategy_id") == sid)
                           .order_by(AgentRun.id.desc()).limit(1)).first()
    since = _utc(last.created_at) if last is not None else None
    fs = [f for f in facts if since is None or f["opened_at"] >= since]
    rs = [f["r"] for f in fs if f.get("r") is not None]
    return {"since": since.isoformat() if since else None, "trades": len(fs),
            "r_sum": round(sum(rs), 2), "pnl": round(sum(f.get("pnl") or 0.0 for f in fs), 2),
            "won": round(sum(1 for f in fs if (f.get("pnl") or 0) > 0) / len(fs), 3) if fs else None,
            "fair_after": 50}


def build(session: Session, cfg: StrategyConfig, config: dict, now: datetime | None = None) -> dict:
    """Everything the board's "What the strategy learned" card shows for one strategy. ``config``: the strategy
    as plain data (strategy_routes._config_of), to tell which lessons it already follows."""
    now = now or datetime.now(timezone.utc)
    facts = facts_of(session, cfg.id)
    with_r = [f for f in facts if f.get("r") is not None]
    dec = decisions(session, cfg.id)
    parts = find_parts(with_r) if len(with_r) >= MIN_TOTAL else []
    from app.core.state import get_or_create_risk_config

    rules = (int(cfg.cooldown_minutes or 0), int(cfg.loss_cooldown_minutes if cfg.loss_cooldown_minutes is not None else 5),
             int(get_or_create_risk_config(session).max_open_positions or 0))
    table = whatif_table(with_r, cfg.target_r, rules)
    lessons = [dict(p) for p in parts if p["kind"]]
    wl = _whatif_lesson(table)
    if wl:
        lessons.append(wl)
    for p in lessons:
        p["title"] = title_of(p)
        p["action"] = action_for(p, config)
        st = dec["state"].get(p["key"])
        p["status"] = st["status"] if st else None
        p["first_seen"] = dec["seen"].get(p["key"])
    lessons.sort(key=lambda p: (p["strength"] != "strong", -abs(p["t"])))
    applied = []
    for key, st in dec["state"].items():
        if st["status"] != "applied":
            continue
        at = datetime.fromisoformat(st["at"])
        after = [f["r"] for f in with_r if f["opened_at"] >= at]
        before = [f["r"] for f in with_r if f["opened_at"] < at]
        applied.append({"key": key, "title": st.get("title"), "at": st["at"], "changes": st.get("changes"),
                        "since": {"n": len(after), "avg_r": round(sum(after) / len(after), 3) if after else None,
                                  "r_sum": round(sum(after), 2)},
                        "before": {"n": len(before), "avg_r": round(sum(before) / len(before), 3) if before else None}})
    return {
        "strategy_id": cfg.id, "now": now.isoformat(),
        "trades": len(facts), "with_r": len(with_r), "with_fp": sum(1 for f in facts if f.get("fp")),
        "with_whatif": sum(1 for f in facts if f.get("whatif")),
        "since": facts[0]["opened_at"].isoformat() if facts else None,
        "total": _summary(with_r) if with_r else None,
        "min_total": MIN_TOTAL, "min_trades": MIN_TRADES, "strong_t": STRONG_T,
        "version": _version(session, cfg.id, facts),
        "lessons": [p for p in lessons if p["status"] != "ignored"],
        "ignored": [p for p in lessons if p["status"] == "ignored"],
        "parts": parts,
        "whatif": table,
        "applied": applied,
    }


# --------------------------------------------------------------------------- #
#  The tick
# --------------------------------------------------------------------------- #

def _book(session: Session) -> list:
    from app.api.journal_routes import _journal_book

    return _journal_book(session)


def learning_tick(session: Session, now: datetime | None = None, broker_for=None, config_of=None,
                  book=None) -> dict:
    """Bring every strategy's facts up to date and note a lesson the first time it turns strong (an AgentRun
    "found", which the board marks new). Reads the broker's history only; changes no strategy."""
    from app.agents.strategy import strategy_source

    now = now or datetime.now(timezone.utc)
    cfgs = session.scalars(select(StrategyConfig).order_by(StrategyConfig.id)).all()
    if not cfgs:
        return {"strategies": 0}
    if broker_for is None:
        from app.brokers.registry import get_broker_for
        from app.core.state import get_or_create_settings
        from app.models.schemas import AssetClass

        settings = get_or_create_settings(session)

        def broker_for(ac: str):  # noqa: E306
            return get_broker_for(AssetClass(ac), settings.broker_map)
    if config_of is None:
        from app.api.strategy_routes import _config_of as config_of
    if book is None:
        book = _book(session)
    out = {"strategies": len(cfgs), "new": 0, "filled": 0, "found": []}
    budget = FILL_BUDGET
    for cfg in cfgs:
        src = strategy_source(cfg)
        trades = [(v, row) for v, row in book if (v.source or "") == src]
        if not trades and not session.scalar(select(func.count(StrategyTradeFact.id))
                                             .where(StrategyTradeFact.strategy_id == cfg.id)):
            continue
        out["new"] += sync_facts(session, cfg, trades)
        if budget > 0:
            n = fill_from_history(session, cfg, broker_for, now, budget)
            budget -= n
            out["filled"] += n
        view = build(session, cfg, config_of(cfg), now)
        seen = decisions(session, cfg.id)["seen"]
        for p in view["lessons"] + view["ignored"]:
            if p["strength"] == "strong" and p["key"] not in seen:
                session.add(AgentRun(agent="learning", symbol=None, event="found",
                                     detail={"strategy_id": cfg.id, "name": cfg.name, "key": p["key"],
                                             "title": p["title"], "n": p.get("n"), "r_sum": p.get("r_sum")}))
                out["found"].append(p["key"])
    session.commit()
    return out
