"""Follow the target on a MANUAL trade (2026-11-22, the user: "the same follow-the-target as the strategy, for my
manual trades: a first, soft take profit that I can move by hand, and the fixed one as we did already").

Switched on per trade - the desk's position card, or the scalp ticket's "🎯 follow past TP" once the order fills.
The app keeps the target (the soft one; the user drags it on the chart), and the broker's take profit sits
FOLLOW_SAFETY_R past it as a safety net - moved with it. At the target, the strategy's own rule
(strategy._follow_one): with the 30s SuperTrend on the trade's side the stop is locked FOLLOW_LOCK_R short of the
target and then trails the 30s line, and the trade is closed when the line turns; otherwise it is closed right
there - the target taken, as a take profit would.

One ticket per side: the broker moves and closes a whole side, so a second ticket on it would be moved too. A
strategy's own trade follows with the board's 🎯 setting instead. Kept in AppSettings.manual_follows
({"SYMBOL|side": entry}), so it survives a restart.

The levels are the user's settings (AppSettings.manual_follow_cfg, 2026-11-22: "a place to set the first and second
stop loss and take profit, and switch it on by itself when I open a trade"): SL1 the trade's stop (theirs, or x ATR
1m), TP1 the target the app keeps (theirs, or x R from the entry), TP2 the broker's safety take profit (x R past
TP1), SL2 the lock at TP1 (x R short of it). With auto on, every NEW manual trade - the scalp ticket's, or placed in
MT5 or on the phone - is followed AUTO_GRACE_S after it opens (time to set its levels by hand); switched off on a
trade, that trade is left manual for good. With the backend, the PC or the MT5 link down, the trade has its
stop and the far safety target only - the same as a strategy trade that follows.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.db import AgentRun, Position
from app.models.enums import AssetClass, PositionStatus

log = get_logger("agents.manual_follow")

# the API's start / move / off / settings and the every-second tick edit the same entries; re-entrant, because the
# tick switches auto trades on (start) while it holds it
_LOCK = threading.RLock()

DEFAULTS = {"auto": False, "sl1_mode": "keep", "sl1_atr": 1.5, "tp1_mode": "keep", "tp1_r": 1.0, "tp2_r": 2.0,
            "sl2_r": 0.1}
LIMITS = {"sl1_atr": (0.3, 10.0), "tp1_r": (0.2, 10.0), "tp2_r": (0.5, 10.0), "sl2_r": (0.0, 1.0)}
AUTO_GRACE_S = 15            # a new manual trade is followed this long after it opens: time to set SL / TP by hand
_AUTO_TRIED: set[str] = set()   # trades auto could not follow (refused once, logged once) - this run only


class FollowRefused(Exception):
    """Why a manual follow can't be switched on, moved or off - said to the user as it is."""


def _norm(symbol: str) -> str:
    from app.risk.service import _norm_symbol

    return _norm_symbol(symbol)


def _key(symbol: str, side: str) -> str:
    return f"{_norm(symbol)}|{side}"


def _entries(session: Session) -> dict:
    from app.core.state import get_or_create_settings

    return dict(get_or_create_settings(session).manual_follows or {})


def _save(session: Session, entries: dict) -> None:
    from app.core.state import get_or_create_settings

    get_or_create_settings(session).manual_follows = entries      # a new dict, so the JSON column is written
    session.commit()


def levels(session: Session) -> dict[tuple[str, str], dict]:
    """(normalized symbol, side) -> the entry, for GET /positions/live (the chart's 🎯 line, the card's rail)."""
    out = {}
    for k, fe in _entries(session).items():
        sym, _, side = k.partition("|")
        out[(sym, side)] = fe
    return out


def _broker(session: Session, asset_class: str):
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings

    return get_broker_for(AssetClass(asset_class or "forex"), get_or_create_settings(session).broker_map)


def _tickets(broker, symbol: str, side: str) -> list:
    """The broker's open tickets on ``symbol``'s ``side``. Raises when the broker can't be read."""
    norm = _norm(symbol)
    return [p for p in broker.get_open_positions() if _norm(p.symbol) == norm and p.direction == side]


def _log(session: Session, symbol: str, event: str, **detail) -> None:
    session.add(AgentRun(agent="manual", symbol=symbol, event=event, level="INFO", detail=detail))


def _mirror(session: Session, symbol: str, side: str, stop: float | None = None, tp: float | None = None,
            risk_zero: bool = False) -> None:
    """The app's own open rows for the trade take the levels the broker now has (as /positions/sl-tp does)."""
    norm = _norm(symbol)
    for r in session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value)).all():
        if _norm(r.symbol) != norm or r.direction != side:
            continue
        if stop is not None:
            r.stop_loss = stop
        if tp is not None:
            r.take_profit = tp
        if risk_zero:
            r.risk_amount = 0.0


def _cfg_raw(session: Session) -> dict:
    from app.core.state import get_or_create_settings

    return dict(get_or_create_settings(session).manual_follow_cfg or {})


def _cfg_save(session: Session, raw: dict) -> None:
    from app.core.state import get_or_create_settings

    get_or_create_settings(session).manual_follow_cfg = raw
    session.commit()


def settings(session: Session) -> dict:
    """The user's levels and the auto switch, defaults filled in."""
    raw = _cfg_raw(session)
    return {k: raw.get(k, v) for k, v in DEFAULTS.items()}


def _fp(p) -> str:
    """One broker trade (the open views carry no ticket): symbol, side and the moment it opened."""
    at = p.opened_at.isoformat() if getattr(p, "opened_at", None) else f"@{p.entry_price}"
    return f"{_norm(p.symbol)}|{p.direction}|{at}"


def _is_manual(session: Session, symbol: str, side: str) -> bool:
    """Opened by the user: the app's row says "manual" (the scalp ticket, the desk), or there is none (MT5, the
    phone). A strategy's, the Hybrid's or any other automation's is not."""
    rows = [r.source for r in session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value))
            if _norm(r.symbol) == _norm(symbol) and r.direction == side]
    return all((src or "manual") == "manual" for src in rows)


def save_settings(session: Session, patch: dict) -> dict:
    """Change the levels / the auto switch. Switching auto on leaves the manual trades open right now alone: it is
    for the trades opened from then on."""
    cur = settings(session)
    new = dict(cur)
    for k, v in (patch or {}).items():
        if k not in DEFAULTS or v is None:
            continue
        if k == "auto":
            new[k] = bool(v)
        elif k in ("sl1_mode", "tp1_mode"):
            ok = ("keep", "atr") if k == "sl1_mode" else ("keep", "r")
            if v not in ok:
                raise FollowRefused(f"{k} must be one of {', '.join(ok)}")
            new[k] = v
        else:
            lo, hi = LIMITS[k]
            x = float(v)
            if not lo <= x <= hi:
                raise FollowRefused(f"{k} must be between {lo:g} and {hi:g}")
            new[k] = round(x, 3)
    with _LOCK:
        raw = _cfg_raw(session)
        if new["auto"] and not cur["auto"]:
            try:
                book = _broker(session, "forex").get_open_positions()
            except Exception:  # noqa: BLE001 - nothing to leave alone that can be seen
                book = []
            raw["skip"] = sorted(_fp(p) for p in book)
            raw["declined"] = []
            _AUTO_TRIED.clear()
        raw.update(new)
        _cfg_save(session, raw)
        _log(session, "", "follow_settings", **{k: new[k] for k in DEFAULTS}, was={k: cur[k] for k in DEFAULTS if cur[k] != new[k]})
        session.commit()
    return settings(session)


def _atr1m(broker, symbol: str) -> float | None:
    from app.agents.indicators import atr
    from app.data.ohlcv_cache import get_ohlcv_cached

    try:
        candles = list(get_ohlcv_cached(broker, symbol, "1m", 60).candles)
    except Exception:  # noqa: BLE001
        return None
    return atr(candles[:-1] if len(candles) > 20 else candles, 14)


def _check_target(side: str, target: float, price: float, entry: float, room: float) -> None:
    s = 1 if side == "long" else -1
    if s * (target - entry) <= 0:
        raise FollowRefused(f"the target must be on the profit side of the entry ({'above' if s > 0 else 'below'} "
                            f"{entry:g})")
    if s * (target - price) <= room:
        raise FollowRefused(f"the target must stay {'above' if s > 0 else 'below'} the current price ({price:g}) - "
                            "to take the profit now, close the trade")


def start(session: Session, symbol: str, asset_class: str, side: str, target: float | None = None,
          auto: bool = False, fp: str | None = None) -> dict:
    """Switch it on for the trade on ``symbol``'s ``side``, on the user's levels (``settings``): SL1 its stop (or x ATR
    1m), TP1 ``target`` / its take profit (or x R), TP2 the broker's take profit x R past TP1, SL2 the lock x R short
    of TP1. Already on: left as it is. Raises FollowRefused with the reason."""
    from app.agents.strategy import _quote, follow_levels

    existing = _entries(session).get(_key(symbol, side))
    if existing:
        return existing
    cfg = settings(session)
    from app.api.settings_routes import _fill_initial_stops

    if side not in ("long", "short"):
        raise FollowRefused("which side - long or short?")
    s = 1 if side == "long" else -1
    if any(_norm(sym) == _norm(symbol) and sd == side for sym, sd in follow_levels(session)):
        raise FollowRefused("a strategy already keeps this trade's target (its board's 🎯 follow past it)")
    owners = {r.source for r in session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value))
              if _norm(r.symbol) == _norm(symbol) and r.direction == side and r.source}
    if any(o.startswith("strategy:") for o in owners):
        raise FollowRefused("this is a strategy's trade - it follows its target with the board's 🎯 follow past it")
    broker = _broker(session, asset_class)
    try:
        held = _tickets(broker, symbol, side)
    except Exception as exc:  # noqa: BLE001
        raise FollowRefused(f"the broker's positions can't be read now: {exc}") from exc
    if not held:
        raise FollowRefused(f"no open {side} on {symbol}")
    if len(held) > 1:
        raise FollowRefused(f"{len(held)} {side} tickets are open on {symbol} - the broker moves and closes a whole "
                            "side, so following one would move the others too")
    pos = held[0]
    try:
        _fill_initial_stops(session, [pos])
    except Exception:  # noqa: BLE001 - the current stop then
        pass
    q = _quote(broker, pos.symbol)
    if not q:
        raise FollowRefused(f"no live quote for {symbol} now")
    price = float(q["bid"] if s > 0 else q["ask"])
    digits = int(q.get("digits") or 6)
    room = float(q.get("stops_level") or 0.0)
    entry = float(pos.entry_price)
    # SL1: the trade's own stop (R from the stop it opened with), unless the settings put it at x ATR - or it has none.
    risk, new_stop = None, None
    if cfg["sl1_mode"] != "atr":
        for st in (getattr(pos, "initial_stop_loss", None), pos.stop_loss):
            if st and s * (entry - float(st)) > 0:
                risk = s * (entry - float(st))
                break
    if risk is None:
        a = _atr1m(broker, pos.symbol)
        if not a:
            raise FollowRefused("it needs a stop, and the 1m ATR to place one can't be read now")
        new_stop = round(entry - s * float(cfg["sl1_atr"]) * a, digits)
        if s * (price - new_stop) <= room:
            raise FollowRefused(f"SL1 at {cfg['sl1_atr']:g} x ATR ({new_stop:g}) is already past the price")
        risk = s * (entry - new_stop)
    # TP1: the target given (the chart), else the trade's own take profit - unless the settings put it at x R, or it has none.
    if target is None:
        if cfg["tp1_mode"] == "r" or not pos.take_profit:
            target = entry + s * float(cfg["tp1_r"]) * risk
        else:
            target = float(pos.take_profit)
    target = round(float(target), digits)
    _check_target(side, target, price, entry, room)
    tp2_r, sl2_r = float(cfg["tp2_r"]), float(cfg["sl2_r"])
    safety = round(target + s * tp2_r * risk, digits)
    was_tp, was_sl = pos.take_profit, pos.stop_loss
    with _LOCK:
        res = broker.set_sl_tp(pos.symbol, stop_loss=new_stop, take_profit=safety, direction=side)
        if res.status.value in ("error", "rejected"):
            raise FollowRefused(f"the broker refused {'SL1 / ' if new_stop is not None else ''}the safety take profit "
                                f"at {safety:g}: {res.error}")
        fe = {"symbol": pos.symbol, "asset_class": asset_class, "side": side, "mode": "waiting", "target": target,
              "lock": round(target - s * sl2_r * risk, digits), "risk": risk, "broker_tp": safety,
              "safety_r": tp2_r, "lock_r": sl2_r, "entry": entry, "auto": auto, "fp": fp or _fp(pos),
              "at": datetime.now(timezone.utc).isoformat()}
        entries = _entries(session)
        entries[_key(pos.symbol, side)] = fe
        _mirror(session, pos.symbol, side, tp=safety, stop=new_stop)
        _log(session, pos.symbol, "follow_on", side=side, target=target, broker_tp=safety, lock=fe["lock"],
             was_tp=was_tp, stop=new_stop, was_sl=was_sl, auto=auto)
        _save(session, entries)
    log.warning("manual follow on", extra={"symbol": pos.symbol, "side": side, "target": target})
    return fe


def move(session: Session, symbol: str, side: str, target: float) -> dict:
    """The user moved the soft target (the chart's 🎯 line): the lock and the broker's safety take profit move
    with it. Only while it waits for the target."""
    from app.agents.strategy import FOLLOW_LOCK_R, FOLLOW_SAFETY_R, _quote

    key = _key(symbol, side)
    fe = dict(_entries(session).get(key) or {})
    if not fe:
        raise FollowRefused("follow is not on for this trade")
    if fe.get("mode") == "following":
        raise FollowRefused("it is past its target already - the stop follows the 30s SuperTrend now")
    if fe.get("mode") == "shared":
        raise FollowRefused("another ticket shares its side, so the app leaves it to its stop and safety target")
    s = 1 if side == "long" else -1
    broker = _broker(session, fe.get("asset_class") or "forex")
    q = _quote(broker, fe["symbol"])
    if not q:
        raise FollowRefused(f"no live quote for {symbol} now")
    price = float(q["bid"] if s > 0 else q["ask"])
    digits = int(q.get("digits") or 6)
    target = round(float(target), digits)
    _check_target(side, target, price, float(fe.get("entry") or target - s), float(q.get("stops_level") or 0.0))
    risk = float(fe["risk"])
    safety_r, lock_r = float(fe.get("safety_r", FOLLOW_SAFETY_R)), float(fe.get("lock_r", FOLLOW_LOCK_R))
    safety = round(target + s * safety_r * risk, digits)
    with _LOCK:
        res = broker.set_sl_tp(fe["symbol"], stop_loss=None, take_profit=safety, direction=side)
        if res.status.value in ("error", "rejected"):
            raise FollowRefused(f"the broker refused the safety take profit at {safety:g}: {res.error}")
        entries = _entries(session)
        was = (entries.get(key) or {}).get("target")
        fe.update(target=target, lock=round(target - s * lock_r * risk, digits), broker_tp=safety)
        entries[key] = fe
        _mirror(session, fe["symbol"], side, tp=safety)
        _log(session, fe["symbol"], "follow_target_moved", side=side, frm=was, to=target, broker_tp=safety)
        _save(session, entries)
    return fe


def stop(session: Session, symbol: str, side: str) -> dict:
    """Switch it off. Still waiting: the broker's take profit goes back to the target, so the trade is as it was.
    Past the target: the stop stays where it is (locked / trailed) and so does the safety take profit."""
    key = _key(symbol, side)
    fe = dict(_entries(session).get(key) or {})
    if not fe:
        return {"off": True}
    restored = None
    if fe.get("mode") in ("waiting", None):
        broker = _broker(session, fe.get("asset_class") or "forex")
        res = broker.set_sl_tp(fe["symbol"], stop_loss=None, take_profit=float(fe["target"]), direction=side)
        if res.status.value == "error":
            raise FollowRefused(f"the take profit could not be put back at {fe['target']:g}: {res.error} - follow "
                                "stays on")
        if res.status.value != "rejected":
            restored = float(fe["target"])
            _mirror(session, fe["symbol"], side, tp=restored)
    with _LOCK:
        entries = _entries(session)
        entries.pop(key, None)
        if fe.get("fp"):
            raw = _cfg_raw(session)
            raw["declined"] = sorted(set(raw.get("declined") or []) | {fe["fp"]})
            _cfg_save(session, raw)
        _log(session, fe["symbol"], "follow_off", side=side, mode=fe.get("mode"), tp=restored)
        _save(session, entries)
    return {"off": True, "take_profit": restored}


def _close(session: Session, fe: dict, why: str) -> bool:
    """Close the trade's side at the broker and book the app's rows (the desk's own close)."""
    from fastapi import HTTPException

    from app.api.settings_routes import LiveCloseRequest, live_close

    try:
        live_close(LiveCloseRequest(symbol=fe["symbol"], asset_class=AssetClass(fe.get("asset_class") or "forex"),
                                    direction=fe["side"]), session)
    except HTTPException as exc:
        _log(session, fe["symbol"], "follow_close_failed", side=fe["side"], reason=str(exc.detail), why=why)
        session.commit()
        return False
    _log(session, fe["symbol"], "follow_closed", side=fe["side"], why=why)
    session.commit()
    log.warning("manual follow closed a trade", extra={"symbol": fe["symbol"], "side": fe["side"], "why": why})
    return True


def _one(session: Session, key: str, fe: dict, now: datetime, books: dict) -> tuple[dict | None, dict | None]:
    """One entry, the strategy's rule (strategy._follow_one). Returns (the entry to keep or None, what happened)."""
    from app.agents.strategy import FOLLOW_TF, _follow_read, _quote, bar_mark

    side = fe["side"]
    s = 1 if side == "long" else -1
    ac = fe.get("asset_class") or "forex"
    if fe.get("wait_until") and now.isoformat() < fe["wait_until"]:
        return fe, None
    broker = _broker(session, ac)
    if ac not in books:
        try:
            books[ac] = broker.get_open_positions()
        except Exception:  # noqa: BLE001 - the book can't be read: nothing is decided on it
            books[ac] = None
    if books[ac] is None:
        return fe, None
    norm = _norm(fe["symbol"])
    held = [p for p in books[ac] if _norm(p.symbol) == norm and p.direction == side]
    if not held:
        _log(session, fe["symbol"], "follow_ended", side=side, reason="the trade is closed")
        return None, {"symbol": fe["symbol"], "ended": side}
    q = _quote(broker, fe["symbol"])
    if not q:
        return fe, None
    price = float(q["bid"] if s > 0 else q["ask"])
    digits = int(q.get("digits") or 6)
    room = float(q.get("stops_level") or 0.0)
    stop_now = held[0].stop_loss

    def take(why: str):
        if _close(session, fe, why):
            return None, {"symbol": fe["symbol"], "closed": side, "why": why}
        return {**fe, "wait_until": (now + timedelta(seconds=10)).isoformat()}, None     # again in 10 s

    if fe.get("mode") in ("waiting", None):
        target = float(fe["target"])
        if s * (price - target) < 0:
            return fe, None
        if len(held) > 1:
            _log(session, fe["symbol"], "follow_shared", side=side, price=price, target=target)
            return {**fe, "mode": "shared"}, None
        read = _follow_read(broker, fe["symbol"], now)
        if read is None or read[0] != s:
            return take(f"target reached - the {FOLLOW_TF} SuperTrend is not with the trade, so it is taken")
        lock = round(float(fe["lock"]), digits)
        if s * (price - lock) <= room:
            return take("target reached - too close to the price to lock the stop, so it is taken")
        if stop_now is not None and s * (lock - float(stop_now)) <= 0:
            lock = float(stop_now)                  # already past the lock (moved by hand): left as it is
        else:
            res = broker.set_sl_tp(fe["symbol"], stop_loss=lock, take_profit=None, direction=side)
            if res.status.value in ("error", "rejected"):
                _log(session, fe["symbol"], "stop_move_failed", side=side, reason=res.error, to=lock)
                return take("target reached - the stop could not be locked, so it is taken")
            _mirror(session, fe["symbol"], side, stop=lock, risk_zero=True)
        _log(session, fe["symbol"], "follow_started", side=side, price=price, target=target, stop=lock, line=read[1])
        log.warning("manual follow past a target", extra={"symbol": fe["symbol"], "side": side})
        return ({**fe, "mode": "following", "mark": bar_mark(FOLLOW_TF, now), "since": now.isoformat(), "lock": lock,
                 "wait_until": None}, {"symbol": fe["symbol"], "following": side})
    if fe.get("mode") != "following":
        return fe, None                             # "shared": nothing the app may do on that side
    mark = bar_mark(FOLLOW_TF, now)
    if mark == fe.get("mark"):
        return fe, None
    read = _follow_read(broker, fe["symbol"], now)
    if read is None:
        return fe, None
    fe = {**fe, "mark": mark}
    if read[0] != s:
        return take(f"following past the target: the {FOLLOW_TF} SuperTrend turned against the trade")
    if read[1] is None:
        return fe, None
    level = round(read[1], digits)
    if (stop_now is not None and s * (level - float(stop_now)) <= 0) or s * (price - level) <= room:
        return fe, None
    res = broker.set_sl_tp(fe["symbol"], stop_loss=level, take_profit=None, direction=side)
    if res.status.value in ("error", "rejected"):
        _log(session, fe["symbol"], "stop_move_failed", side=side, reason=res.error, to=level)
        return fe, None
    _mirror(session, fe["symbol"], side, stop=level)
    _log(session, fe["symbol"], "stop_moved", side=side, frm=stop_now, to=level, line=read[1], tf=FOLLOW_TF,
         follow=True)
    return fe, {"symbol": fe["symbol"], "stop": level}


def tick(session: Session, now: datetime | None = None) -> list[dict]:
    """Every second (strategy.strategy_tick, before the strategies): each manual trade that follows its target."""
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        out = _auto(session, now)
        entries = _entries(session)
        if not entries:
            return out
        books, changed = {}, False
        for key in list(entries):
            try:
                keep, did = _one(session, key, dict(entries[key]), now, books)
            except Exception as exc:  # noqa: BLE001 - one trade never stops the others
                log.warning("manual follow failed", extra={"key": key, "error": str(exc)})
                continue
            if keep is None:
                entries.pop(key, None)
                changed = True
            elif keep != entries[key]:
                entries[key] = {k: v for k, v in keep.items() if v is not None}
                changed = True
            if did:
                out.append(did)
        if changed:
            _save(session, entries)
        else:
            session.commit()
    return out


def _auto(session: Session, now: datetime) -> list[dict]:
    """With auto on: follow every NEW manual trade AUTO_GRACE_S after it opens, on the user's levels. Not the ones open
    when auto was switched on ("skip"), nor one the user switched off ("declined"), nor a strategy's / automation's."""
    from app.agents.strategy import follow_levels

    cfg = settings(session)
    if not cfg["auto"]:
        return []
    try:
        book = _broker(session, "forex").get_open_positions()
    except Exception:  # noqa: BLE001 - the book can't be read: nothing is decided on it
        return []
    raw = _cfg_raw(session)
    skip, declined = set(raw.get("skip") or []), set(raw.get("declined") or [])
    entries = _entries(session)
    kept = {(_norm(sym), sd) for sym, sd in follow_levels(session)}
    sides = {}
    for p in book:
        sides[(_norm(p.symbol), p.direction)] = sides.get((_norm(p.symbol), p.direction), 0) + 1
    out, open_fps = [], set()
    for p in book:
        if p.direction not in ("long", "short"):
            continue
        fp = _fp(p)
        open_fps.add(fp)
        if fp in skip or fp in declined or fp in _AUTO_TRIED or _key(p.symbol, p.direction) in entries:
            continue
        if (_norm(p.symbol), p.direction) in kept or sides[(_norm(p.symbol), p.direction)] > 1:
            continue
        if p.opened_at and (now - p.opened_at).total_seconds() < AUTO_GRACE_S:
            continue
        if not _is_manual(session, p.symbol, p.direction):
            _AUTO_TRIED.add(fp)
            continue
        try:
            start(session, p.symbol, p.asset_class, p.direction, auto=True, fp=fp)
            out.append({"symbol": p.symbol, "auto": p.direction})
        except FollowRefused as exc:
            _AUTO_TRIED.add(fp)
            _log(session, p.symbol, "auto_follow_refused", side=p.direction, reason=str(exc))
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one trade never stops the others
            log.warning("auto follow failed", extra={"symbol": p.symbol, "error": str(exc)})
    # Only the trades still open are remembered.
    if (skip - open_fps) or (declined - open_fps):
        raw = _cfg_raw(session)
        raw["skip"] = sorted(skip & open_fps)
        raw["declined"] = sorted(declined & open_fps)
        _cfg_save(session, raw)
    return out
