"""Watch-only signals, judged - and the strategy edits they can be judged against (asked 2026-11-08: "keep track
in a separate place in the journal ... the result of those signals, to learn from it ... so I can enhance it in
the strategy ... after I change I need a brief result of those signals if it was applied").

Each "signal" a strategy group on watch logged (strategy._watch) is replayed as the trade it would have been:
in at its price (the ask for a buy, the bid for a sell, as logged), out at the strategy's own stop or target -
whichever price reaches first after it - or, still open a day later, where price is then. Since 2026-11-08 a
signal logs the stop and the target the strategy would have placed (``levels`` "logged"); the earlier ones are
given them from the strategy's CURRENT stop and target settings and the ATR at the time (``levels``
"estimated"). The strategy's close rules are not replayed: a real trade could leave earlier, either way.

Read from the broker's own history: the ticks of the signal's first minute (a buy leaves on the bid, a sell on
the ask), then 1-minute candles (the bid; a sell adds the spread). Both levels inside one candle count as the
STOP - the cautious reading. An outcome that can no longer change is kept in memory; nothing is written.

``describe_changes`` turns two snapshots of a strategy into the lines an edit records ("BUY group 2: + ADX ...",
"stop 4.5x ATR -> 4x"), so the journal can say what changed before the signals that followed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.sessions import name_of as session_name
from app.models.db import AgentRun, StrategyConfig
from app.models.schemas import Candle

log = logging.getLogger(__name__)

HORIZON_S = 24 * 3600          # still open a day on: judged where price is then
_FINAL: dict[int, dict] = {}   # signal id -> its outcome, once it can no longer change
_MAX_CHANGES = 24              # lines an edit keeps


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
#  The replay
# --------------------------------------------------------------------------- #

def simulate(side: int, entry: float, stop: float, target: float | None,
             path: list[tuple[datetime, float, float, float, float]],
             horizon_end: datetime, now: datetime) -> dict:
    """Walk ``path`` - (time, high, low, close, spread) of BID prices, in order, from the signal on - until the
    stop or the target is reached. A buy leaves on the bid, a sell on the ask (bid + spread). Returns the result
    ("win" / "loss" / "timeout" / "open"), its R, where and when it left, and how far it went for (mfe_r) and
    against (mae_r) it on the way, in R. Both levels inside one step = the stop (``same_bar``)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return {"result": "unknown", "why": "no distance to the stop"}
    mfe = mae = 0.0
    last_close = None
    last_t = None
    for t, hi, lo, close, spread in path:
        if t > horizon_end:
            break
        if side > 0:
            out_hi, out_lo, out_close = hi, lo, close
        else:
            out_hi, out_lo, out_close = hi + spread, lo + spread, close + spread
        fav = (out_hi - entry) / risk if side > 0 else (entry - out_lo) / risk
        adv = (entry - out_lo) / risk if side > 0 else (out_hi - entry) / risk
        hit_stop = out_lo <= stop if side > 0 else out_hi >= stop
        hit_tgt = target is not None and (out_hi >= target if side > 0 else out_lo <= target)
        if hit_stop:
            # Which came first inside this step is unknown: its own best is left out of mfe.
            return {"result": "loss", "r": -1.0, "exit_at": t.isoformat(), "exit_price": stop,
                    "mfe_r": round(mfe, 2), "mae_r": 1.0, "same_bar": bool(hit_tgt)}
        if hit_tgt and target is not None:
            r = abs(target - entry) / risk
            return {"result": "win", "r": round(r, 2), "exit_at": t.isoformat(), "exit_price": target,
                    "mfe_r": round(max(mfe, r), 2), "mae_r": round(max(mae, adv), 2), "same_bar": False}
        mfe, mae = max(mfe, fav), max(mae, adv)
        last_close, last_t = out_close, t
    if last_close is None:
        return {"result": "open", "r": None, "mfe_r": 0.0, "mae_r": 0.0}
    r = round(side * (last_close - entry) / risk, 2)
    done = now >= horizon_end
    return {"result": "timeout" if done else "open", "r": r, "exit_at": last_t.isoformat() if done else None,
            "exit_price": last_close if done else None, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2)}


# --------------------------------------------------------------------------- #
#  The broker's history
# --------------------------------------------------------------------------- #

def _is_mt5(broker) -> bool:
    return hasattr(broker, "_mt5") and hasattr(broker, "_resolve_symbol") and hasattr(broker, "_tf")


def _ticks(broker, symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
    """(time, bid, ask) of every tick in [start, end) - MT5 only; [] elsewhere or on failure."""
    if not _is_mt5(broker):
        return []
    try:
        mt5 = broker._mt5
        raw = mt5.copy_ticks_range(broker._resolve_symbol(symbol), start, end, mt5.COPY_TICKS_ALL)
    except Exception as exc:  # noqa: BLE001
        log.info("signal review: no ticks", extra={"symbol": symbol, "error": str(exc)})
        return []
    out = []
    for t in raw if raw is not None else []:
        bid, ask = float(t["bid"]), float(t["ask"])
        if bid > 0 and ask > 0:
            out.append((datetime.fromtimestamp(int(t["time_msc"]) / 1000, tz=timezone.utc), bid, ask))
    return out


def _bars(broker, symbol: str, tf: str, start: datetime, end: datetime) -> list[Candle]:
    """The ``tf`` candles that OPENED in [start, end): MT5's history by range, else the latest ones trimmed."""
    if _is_mt5(broker):
        try:
            rates = broker._mt5.copy_rates_range(broker._resolve_symbol(symbol), broker._tf(tf), start, end)
        except Exception as exc:  # noqa: BLE001
            log.info("signal review: no candles", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            rates = None
        out = []
        for r in rates if rates is not None else []:
            ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            if start <= ts < end:
                out.append(Candle(ts=ts, open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                                  close=float(r["close"]), volume=float(r["tick_volume"])))
        return out
    try:
        minutes = int((datetime.now(timezone.utc) - start).total_seconds() // 60) + 5
        series = broker.get_ohlcv(symbol, tf, min(20000, max(10, minutes)))
    except Exception:  # noqa: BLE001
        return []
    return [c for c in series.candles if start <= _utc(c.ts) < end]


def _second_bars_at(ticks: list[tuple[datetime, float, float]], seconds: int) -> list[Candle]:
    """Bars of ``seconds`` built from (time, bid, ask) ticks - on the bid, as MT5 draws them."""
    bars: dict[int, list[float]] = {}
    for t, bid, _ask in ticks:
        k = int(t.timestamp()) // seconds * seconds
        b = bars.get(k)
        if b is None:
            bars[k] = [bid, bid, bid, bid]
        else:
            b[1], b[2], b[3] = max(b[1], bid), min(b[2], bid), bid
    return [Candle(ts=datetime.fromtimestamp(k, tz=timezone.utc), open=o, high=h, low=lo, close=c, volume=0.0)
            for k, (o, h, lo, c) in sorted(bars.items())]


def _estimate_levels(broker, cfg: StrategyConfig, symbol: str, side: int, entry: float, at: datetime,
                     spread: float) -> dict | None:
    """The stop and target the strategy's CURRENT settings would have given a signal logged without them:
    stop_atr x ATR(14) of its base timeframe, read on the bars closed before the signal (never closer than the
    spread), and target_r x that."""
    from app.agents.strategy import TF_SECONDS, Bars, _atr14

    tf = cfg.timeframe or "1m"
    sec = int(TF_SECONDS.get(tf, 60))
    if sec < 60:
        candles = _second_bars_at(_ticks(broker, symbol, at - timedelta(seconds=sec * 40), at), sec)
        candles = [c for c in candles if c.ts.timestamp() + sec <= at.timestamp()]
    else:
        candles = _bars(broker, symbol, tf, at - timedelta(seconds=sec * 40), at - timedelta(seconds=sec))
    if len(candles) < 15:
        return None
    a = _atr14(Bars(tf, candles))
    if not a or a <= 0:
        return None
    risk = max(float(cfg.stop_atr or 1.5) * a, spread)
    out = {"stop": entry - side * risk, "risk": risk}
    if cfg.target_r:
        out["target"] = entry + side * float(cfg.target_r) * risk
    return out


def judge(broker, cfg: StrategyConfig | None, symbol: str, side: int, entry: float, at: datetime,
          detail: dict, now: datetime) -> dict:
    """One signal's outcome: its levels (logged, or estimated from the strategy's settings) and the replay."""
    first_min_end = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    ticks = _ticks(broker, symbol, at, first_min_end)
    spreads = sorted(a - b for _t, b, a in ticks)
    spread = spreads[len(spreads) // 2] if spreads else 0.0
    if not spread:
        try:
            spread = float(broker.spread(symbol) or 0.0)
        except Exception:  # noqa: BLE001
            spread = 0.0
    if detail.get("stop") is not None:
        lv = {"stop": float(detail["stop"]), "target": detail.get("target"), "levels": "logged"}
        if lv["target"] is not None:
            lv["target"] = float(lv["target"])
    else:
        est = _estimate_levels(broker, cfg, symbol, side, entry, at, spread) if cfg is not None else None
        if est is None:
            return {"result": "unknown", "levels": "none", "why": "no candles to place its stop by"}
        lv = {"stop": est["stop"], "target": est.get("target"), "levels": "estimated"}
    horizon_end = at + timedelta(seconds=HORIZON_S)
    path: list[tuple[datetime, float, float, float, float]] = [(t, b, b, b, a - b) for t, b, a in ticks]
    end = min(now, horizon_end) + timedelta(minutes=1)
    for c in _bars(broker, symbol, "1m", first_min_end, end):
        path.append((_utc(c.ts), c.high, c.low, c.close, spread))
    out = simulate(side, entry, lv["stop"], lv["target"], path, horizon_end, now)
    return {**out, **lv, "spread": spread}


# --------------------------------------------------------------------------- #
#  The review
# --------------------------------------------------------------------------- #

def review(session: Session, days: int, now: datetime | None = None, broker_for=None) -> dict:
    """Every watch-only signal of the last ``days``, newest first, with what it would have done, and the
    strategies' edits in that time (what changed, when it was recorded) - the journal groups the signals by
    them. ``broker_for(asset_class)``: where its history is read (the configured broker by default)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(tzinfo=None)
    rows = session.scalars(select(AgentRun).where(AgentRun.agent == "strategy",
                                                  AgentRun.event.in_(("signal", "edited")),
                                                  AgentRun.created_at >= since)
                           .order_by(AgentRun.id.desc())).all()
    configs = {c.id: c for c in session.scalars(select(StrategyConfig)).all()}
    classes = {p.get("symbol"): p.get("asset_class") for c in configs.values() for p in (c.pairs or [])}
    if broker_for is None:
        from app.brokers.registry import get_broker_for
        from app.core.state import get_or_create_settings
        from app.models.schemas import AssetClass

        settings = get_or_create_settings(session)

        def broker_for(ac: str):  # noqa: E306
            return get_broker_for(AssetClass(ac), settings.broker_map)

    signals, edits = [], []
    for r in rows:
        d = r.detail or {}
        at = _utc(r.created_at)
        if r.event == "edited":
            edits.append({"id": r.id, "at": at.isoformat(), "strategy_id": d.get("strategy_id"),
                          "name": d.get("name"), "changes": d.get("changes")})
            continue
        side = 1 if d.get("side") == "long" else -1 if d.get("side") == "short" else 0
        price = d.get("price")
        base = {"id": r.id, "at": at.isoformat(), "symbol": r.symbol, "strategy_id": d.get("strategy_id"),
                "name": d.get("name"), "side": d.get("side"), "group": d.get("group"), "price": price,
                "rules": d.get("rules") or []}
        if not side or price is None or not r.symbol:
            signals.append({**base, "result": "unknown", "levels": "none", "why": "no side or price logged"})
            continue
        out = _FINAL.get(r.id)
        if out is None:
            try:
                broker = broker_for(classes.get(r.symbol) or "forex")
                out = judge(broker, configs.get(d.get("strategy_id")), r.symbol, side, float(price), at, d, now)
            except Exception as exc:  # noqa: BLE001
                log.warning("signal review: replay failed", extra={"signal": r.id, "error": str(exc)})
                out = {"result": "unknown", "levels": "none", "why": "its history could not be read"}
            if out.get("result") in ("win", "loss", "timeout"):
                _FINAL[r.id] = out
        signals.append({**base, **out})
    return {"signals": signals, "edits": edits, "horizon_h": HORIZON_S // 3600, "days": days,
            "now": now.isoformat()}


# --------------------------------------------------------------------------- #
#  What an edit changed
# --------------------------------------------------------------------------- #

_SNAP_FIELDS = ("name", "pairs", "timeframe", "sides", "mirror_short", "entry_long", "entry_short", "exit_rules",
                "risk_pct", "stop_atr", "target_r", "max_trades_day", "manage_manual", "cooldown_minutes",
                "loss_cooldown_minutes", "stop", "hours", "daily_target", "off_groups", "live")


def snapshot(row: StrategyConfig) -> dict:
    """The parts of a strategy an edit can change, as plain data."""
    import copy

    return {f: copy.deepcopy(getattr(row, f, None)) for f in _SNAP_FIELDS}


def _num(v) -> str:
    return "none" if v is None else f"{float(v):g}"


def _hour_runs(hours: list) -> str:
    """[8, 9, 10, 16] -> "08:00-11:00, 16:00-17:00": UTC hours picked one by one, as runs."""
    hs = sorted({int(h) for h in hours})
    if not hs:
        return "none"
    runs, start, prev = [], hs[0], hs[0]
    for h in hs[1:] + [None]:
        if h is not None and h == prev + 1:
            prev = h
            continue
        runs.append(f"{start:02d}:00-{(prev + 1) % 24:02d}:00")
        if h is not None:
            start = prev = h
    return ", ".join(runs)


def describe_changes(before: dict, after: dict) -> list[str]:
    """What changed between two snapshots, one short line each - the rules group by group, by the conditions'
    own labels (strategy.label), then the settings. Empty when nothing did."""
    from app.agents.strategy import label

    out: list[str] = []
    sets = [("entry_long", "BUY", "long"), ("exit_rules", "CLOSE", "exit")]
    if not (after.get("mirror_short") and before.get("mirror_short")):
        sets.insert(1, ("entry_short", "SELL", "short"))
    for key, name, where in sets:
        old, new = before.get(key) or [], after.get(key) or []
        for i in range(max(len(old), len(new))):
            if i >= len(old):
                out.append(f"{name} group {i + 1} added ({len(new[i] or [])} conditions)")
                continue
            if i >= len(new):
                out.append(f"{name} group {i + 1} removed")
                continue
            was = [label(c, where) for c in (old[i] or [])]
            now = [label(c, where) for c in (new[i] or [])]
            out += [f"{name} group {i + 1}: + {x}" for x in now if x not in was]
            out += [f"{name} group {i + 1}: − {x}" for x in was if x not in now]
        off_old = set((before.get("off_groups") or {}).get(key) or [])
        off_new = set((after.get("off_groups") or {}).get(key) or [])
        for i in sorted(off_new - off_old):
            out.append(f"{name} group {i + 1}: switched off" + (" (watch only)" if key != "exit_rules" else ""))
        for i in sorted(off_old - off_new):
            out.append(f"{name} group {i + 1}: switched on")
    if bool(before.get("mirror_short")) != bool(after.get("mirror_short")):
        out.append("SELL rules: " + ("the BUY rules mirrored" if after.get("mirror_short") else "their own"))

    def pct(v) -> str:
        return "none" if v is None else f"{float(v) * 100:g}%"

    scalars = [
        ("name", "name", lambda v: str(v)),
        ("timeframe", "base timeframe", lambda v: str(v)),
        ("sides", "sides", lambda v: str(v)),
        ("risk_pct", "risk per trade", pct),
        ("stop_atr", "stop", lambda v: f"{_num(v)}× ATR"),
        ("target_r", "target", lambda v: "none" if v is None else f"{_num(v)}R"),
        ("max_trades_day", "trades a day", _num),
        ("cooldown_minutes", "wait after a close", lambda v: f"{_num(v)} min"),
        ("loss_cooldown_minutes", "same-side wait after a loss", lambda v: f"{_num(v)} min"),
        ("daily_target", "daily target", lambda v: "none" if v is None else f"${_num(v)}"),
        ("live", "trades the real account", lambda v: "on" if v else "off"),
        ("manage_manual", "manages your own trades", lambda v: "on" if v else "off"),
    ]
    for key, name, fmt in scalars:
        if before.get(key) != after.get(key):
            out.append(f"{name}: {fmt(before.get(key))} → {fmt(after.get(key))}")
    if before.get("stop") != after.get("stop"):
        def stop_text(s) -> str:
            if not s:
                return "ATR stop"
            return f"SuperTrend {s.get('tf')} ({_num(s.get('period'))}, {_num(s.get('mult'))}) + {_num(s.get('buffer'))}× ATR"
        out.append(f"stop placement: {stop_text(before.get('stop'))} → {stop_text(after.get('stop'))}")
    if before.get("hours") != after.get("hours"):
        def hours_text(h) -> str:
            if not h:
                return "around the clock"
            if "hours_utc" in h:
                text = f"hours {_hour_runs(h.get('hours_utc') or [])} UTC"
            elif "sessions" in h:
                text = f"sessions: {' + '.join(session_name(k) for k in h.get('sessions') or []) or 'none'}"
            else:
                text = f"{h.get('start')}-{h.get('end')} {h.get('tz') or ''}".strip()
            return f"{text}, crypto 24/7" if h.get("crypto_24h") else text
        out.append(f"trading hours: {hours_text(before.get('hours'))} → {hours_text(after.get('hours'))}")
    was_p = [p.get("symbol") for p in before.get("pairs") or []]
    now_p = [p.get("symbol") for p in after.get("pairs") or []]
    added, gone = [p for p in now_p if p not in was_p], [p for p in was_p if p not in now_p]
    if added:
        out.append("+ pairs: " + ", ".join(added))
    if gone:
        out.append("− pairs: " + ", ".join(gone))
    if len(out) > _MAX_CHANGES:
        out = out[:_MAX_CHANGES - 1] + [f"… and {len(out) - _MAX_CHANGES + 1} more"]
    return out
