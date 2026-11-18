"""Advice for a SCALP: an open trade that is not a swing plan (2026-11-17).

The user: "the advisor gives me nothing useful, and it compares with the DAY while we scalp on a small timeframe."

Checked first on strategy 1's 281 closed trades, replayed minute by minute on MT5's own 1m bars with the swing
advisor's own code (study 2026-11-17, scratchpad adv/):
  - its trend reads were noise on these trades. "Losing steam" showed at 58% of all moments and on 231 of the 281
    trades; closing at its first warning would have cost 0.06R a trade against holding - no better than closing at a
    random minute (0.03-0.05R) - and at "trend flipped" 0.19R (12 trades). The daily chart's lean changed nothing:
    trades with it ended +0.02R, against it +0.00R.
  - where the trade stands is what carried information. Trades ahead by +0.3R or more, 5 to 25 minutes in, ended in
    profit 83% of the time where the distances alone say 74%, and holding them beat closing by 0.09R a trade (in
    both halves of the history). Trades behind by 0.3R or more: 16% against 27%; closing them saved 0.08R a trade,
    a weak hint only.

So a scalp is read from its own numbers, not a trend:
  - the odds it ends in profit: from the strategy's own past trades that stood at the same R at about the same age,
    when there are MIN_HISTORY of them (the learner keeps each closed trade's minute-by-minute path:
    learning.trade_path), else from the distances to the stop and the target alone;
  - what holding from here paid against closing, in those same trades, and how sure that is;
  - how long trades this old still took;
  - the real risks for a trade of a few minutes: no stop, high-impact news within NEWS_BEFORE_MIN, the market
    closing within CLOSE_WARN_MIN, a spread eating SPREAD_ROOM of the room left to the stop.
A trade planned on 1h or above (Hybrid, the 1h analysis) keeps the swing read (position_advisor). Advice only:
nothing here moves an order.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from statistics import median

from app.core.logging import get_logger
from app.models.schemas import PositionAdvice

log = get_logger("agents.scalp_advice")

SWING_TFS = ("1h", "2h", "3h", "4h", "6h", "8h", "12h", "1d", "d1", "1w", "w1")
NEWS_BEFORE_MIN = 45        # a scalp lasts ~16 min (median), 90% are done within ~50
NEWS_AFTER_MIN = 15
CLOSE_WARN_MIN = 30
SPREAD_ROOM = 0.30          # the spread is this share of the room left to the stop (or more)
AHEAD_R = 0.3
BEHIND_R = -0.3
MIN_HISTORY = 20            # past trades behind the strategy's own odds
PRIOR_N = 20                # ... weighted toward the distances' odds like this many trades: 20 of 27 alone is 74%
#                             give or take 9 - the figure shown leans on the distances until the sample is large
R_WINDOWS = (0.12, 0.2)     # how near in R a past moment must be (the wider one only when the near one is short)
AGE_SPAN = 1.5              # ... and in age: within x1.5 either way
MIN_LEFT = 15
SURE_T = 2.0                # |t| for "a solid edge"; under 1 = no real difference
PLAN_MATCH_S = 600          # a proposal this near the trade's open time is the plan that opened it
CACHE_S = 120               # the desk reads the advice every 15 s: the history is read at most every 2 min
_CACHE: dict[int, tuple[float, list]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_swing(tf: str | None) -> bool:
    return (tf or "").strip().lower() in SWING_TFS


def strategy_id(source: str | None) -> int | None:
    s = source or ""
    if not s.startswith("strategy:"):
        return None
    try:
        return int(s.split(":", 1)[1])
    except ValueError:
        return None


def plain_odds(direction: str, price: float | None, stop: float | None, target: float | None) -> float | None:
    """Odds of reaching the target before the stop from the distances alone - a market with no lean either way."""
    if price is None or not stop or not target:
        return None
    s = 1 if direction == "long" else -1
    down, up = s * (price - stop), s * (target - price)
    if down + up <= 0:
        return None
    return max(0.0, min(1.0, down / (down + up)))


def _moments(facts: list[dict]) -> list[tuple[list[tuple[float, float]], float, float | None]]:
    out = []
    for f in facts:
        path, r = f.get("path"), f.get("r")
        if not path or r is None:
            continue
        o, c = f.get("opened_at"), f.get("closed_at")
        held = (c - o).total_seconds() / 60 if (o and c) else None
        out.append(([(float(m), float(x)) for m, x in path], float(r), held))
    return out


def history_of(session, sid: int) -> list:
    """The strategy's closed trades as (path, final R, minutes held), cached CACHE_S."""
    hit = _CACHE.get(sid)
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_S:
        return hit[1]
    from app.agents.learning import facts_of

    moms = _moments(facts_of(session, sid))
    _CACHE[sid] = (now, moms)
    return moms


def history_odds(moms: list, age_min: float, r_now: float) -> dict | None:
    """The strategy's past trades that stood at about ``r_now`` at about this age (each trade once: its nearest
    moment in age): how many ended in profit, and what holding from there paid against closing then."""
    age = max(1.0, age_min)
    picked: list[tuple[float, float]] = []
    for w in R_WINDOWS:
        picked = []
        for path, final, _held in moms:
            best = None
            for m, r in path:
                if age / AGE_SPAN <= m <= age * AGE_SPAN and abs(r - r_now) <= w:
                    d = abs(math.log(m / age))
                    if best is None or d < best[0]:
                        best = (d, r)
            if best is not None:
                picked.append((best[1], final))
        if len(picked) >= MIN_HISTORY:
            break
    else:
        return None
    n = len(picked)
    gains = [f - r for r, f in picked]
    mean = sum(gains) / n
    sd = math.sqrt(sum((g - mean) ** 2 for g in gains) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "won": sum(1 for _r, f in picked if f > 0), "hold_edge": round(mean, 3),
            "hold_t": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0}


def minutes_left(moms: list, age_min: float) -> float | None:
    """Median time the strategy's trades still open at this age took to close."""
    rest = [h - age_min for _p, _f, h in moms if h is not None and h >= age_min]
    return round(median(rest), 1) if len(rest) >= MIN_LEFT else None


# --------------------------------------------------------------------------- the trade's plan and the market

def plan_of(session, p, dbp):
    """The proposal that opened this trade: the latest EXECUTED one for its symbol and side, made within
    PLAN_MATCH_S of the app's row for it. None for a trade the app has no row of (phone / terminal) - an older
    proposal on the same symbol and side would be another trade's plan."""
    if session is None or dbp is None or not getattr(dbp, "opened_at", None):
        return None
    from app.agents.position_advisor import _plan_proposal
    from app.models.enums import ProposalStatus

    row = _plan_proposal(session, p.symbol, p.direction)
    if row is None or row.status != ProposalStatus.EXECUTED.value or row.direction != p.direction:
        return None
    if not row.created_at or abs((_aware(row.created_at) - _aware(dbp.opened_at)).total_seconds()) > PLAN_MATCH_S:
        return None
    return row


def risk_of(plan, p) -> float | None:
    """One R in price: the planned entry to its stop (what the sidebar's R uses), else the stop the trade opened
    with when the book knows it."""
    if plan is not None and plan.entry and plan.stop_loss and abs(plan.entry - plan.stop_loss) > 0:
        return abs(plan.entry - plan.stop_loss)
    init = getattr(p, "initial_stop_loss", None)
    if init and abs(p.entry_price - init) > 0:
        return abs(p.entry_price - init)
    return None


def broker_of(session, p):
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings
    from app.models.enums import AssetClass

    return get_broker_for(AssetClass(p.asset_class), get_or_create_settings(session).broker_map)


def closes_soon(broker, symbol: str, now: datetime) -> tuple[int, str] | None:
    """(minutes, kind) when the market closes within CLOSE_WARN_MIN - a daily break, overnight or the weekend."""
    from app.data.market_hours import market_hours

    mh = market_hours(broker, symbol, now)
    if mh.get("always_open") or not mh.get("closes_at") or mh.get("open") is False:
        return None
    mins = (datetime.fromisoformat(mh["closes_at"]) - now).total_seconds() / 60
    if 0 <= mins <= CLOSE_WARN_MIN:
        return int(round(mins)), str(mh.get("closure") or "close")
    return None


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _age_text(mins: float) -> str:
    if mins < 1:
        return "under a minute"
    if mins < 90:
        return f"{mins:.0f} min"
    return f"{mins / 60:.1f} h"


_CLOSURE = {"daily_break": "its daily break", "overnight": "the night", "weekend": "the weekend"}


# --------------------------------------------------------------------------- the advice

def advise(session, p, dbp, events: list, now: datetime) -> PositionAdvice:
    sym = p.symbol
    s = 1 if p.direction == "long" else -1
    has_stop = p.stop_loss is not None and p.stop_loss != 0
    has_target = p.take_profit is not None and p.take_profit != 0
    price = p.last_price
    try:
        plan = plan_of(session, p, dbp)
    except Exception:  # noqa: BLE001 - no plan: the read works without one
        plan = None
    risk = risk_of(plan, p)
    r_now = round(s * (price - p.entry_price) / risk, 2) if (risk and price is not None) else None
    opened = getattr(dbp, "opened_at", None) or getattr(p, "opened_at", None)
    age = max(0.0, (now - _aware(opened)).total_seconds() / 60) if opened else None
    source = (getattr(dbp, "source", None) if dbp is not None else None) or getattr(p, "source", None)
    sid = strategy_id(source)

    plain = plain_odds(p.direction, price, p.stop_loss if has_stop else None, p.take_profit if has_target else None)
    hist = left = None
    if sid is not None and age is not None and session is not None:
        try:
            moms = history_of(session, sid)
            hist = history_odds(moms, age, r_now) if r_now is not None else None
            left = minutes_left(moms, age)
        except Exception as exc:  # noqa: BLE001 - the history is a bonus: the distances still answer
            log.info("scalp advice: no history", extra={"symbol": sym, "error": str(exc)})

    # --- the risks that matter for a trade of a few minutes
    ev_label = ev_mins = None
    soft = []
    for e in events or []:
        mins = int((_aware(e.when) - now).total_seconds() / 60)
        imp = str(e.importance).lower()
        if imp == "high" and -NEWS_AFTER_MIN <= mins <= NEWS_BEFORE_MIN and (ev_mins is None or mins < ev_mins):
            ev_label, ev_mins = e.label, mins
        elif imp == "medium" and 0 <= mins <= NEWS_BEFORE_MIN:
            soft.append((mins, e.label))
    soft.sort()
    closing = spread = None
    broker = None
    if session is not None:
        try:
            broker = broker_of(session, p)
        except Exception:  # noqa: BLE001
            broker = None
    if broker is not None:
        try:
            closing = closes_soon(broker, sym, now)
        except Exception:  # noqa: BLE001
            closing = None
        losing_side = has_stop and s * (p.stop_loss - p.entry_price) < 0
        if losing_side and price is not None:
            try:
                sp = broker.spread(sym)
            except Exception:  # noqa: BLE001
                sp = None
            room = s * (price - p.stop_loss)
            if sp and room > 0 and sp / room >= SPREAD_ROOM:
                spread = (sp, sp / room)

    severity, headline = "info", None
    lines: list[str] = []
    if not has_stop:
        severity, headline = "danger", f"{sym} — no stop set"
        lines.append("This trade has no stop-loss, so one fast move can cost a lot. Set a stop now.")
    if ev_label is not None:
        when = f"in about {ev_mins} min" if ev_mins > 0 else "out now"
        if severity != "danger":
            severity = "warn"
        headline = headline or f"{sym} — high-impact news {when}"
        lines.append(f"High-impact news ({ev_label}) is {when}: spreads widen and price can jump past the stop in "
                     "seconds. Holding through it or closing before it is your call - the odds below don't "
                     "include news.")
    if closing is not None:
        mins, kind = closing
        if severity == "info":
            severity = "warn"
        headline = headline or f"{sym} — market closes in {mins} min"
        lines.append(f"The market closes in about {mins} min (for {_CLOSURE.get(kind, 'a while')}): a trade still "
                     "open then waits for the reopen, and the price can open past its stop.")
    if spread is not None:
        sp, share = spread
        if severity == "info":
            severity = "warn"
        headline = headline or f"{sym} — the spread is wide right now"
        lines.append(f"The spread ({sp:.5g}) is {_pct(share)} of the room left to the stop: a spread widening "
                     "alone can close this trade.")

    # --- where it stands, and what trades like it did from here
    close_better = hist is not None and hist["hold_t"] <= -SURE_T
    if headline is None:
        if r_now is None:
            headline = f"{sym} — {'stop and target set' if has_target else 'stop set'}, nothing urgent"
        elif close_better and (r_now >= AHEAD_R or r_now <= BEHIND_R):
            severity, headline = "warn", f"{sym} — trades like this did better closed here"
        elif r_now >= AHEAD_R:
            headline = f"{sym} — ahead: let it run to the target" if has_target else f"{sym} — ahead"
        elif r_now <= BEHIND_R:
            headline = f"{sym} — behind: the stop is the plan"
        else:
            headline = f"{sym} — on plan: nothing to do"

    odds = None
    if hist is not None:
        odds = ((hist["won"] + PRIOR_N * plain) / (hist["n"] + PRIOR_N) if plain is not None
                else hist["won"] / hist["n"])
        where = "ahead" if r_now >= AHEAD_R else "behind" if r_now <= BEHIND_R else "near the entry"
        txt = (f"Odds it ends in profit: about {_pct(odds)} - {hist['won']} of {hist['n']} of this strategy's past "
               f"trades that were {where} like this ({r_now:+.2f}R) at about {_age_text(age)} ended in profit")
        txt += f", and the stop and target distances alone say {_pct(plain)}." if plain is not None else "."
        lines.append(txt)
        e, t = hist["hold_edge"], hist["hold_t"]
        if abs(t) < 1:
            lines.append(f"Holding or closing here has made no real difference in the past ({e:+.2f}R a trade).")
        elif e > 0:
            lines.append(f"Holding from here has paid {e:+.2f}R a trade more than closing at this point "
                         f"({'a solid edge' if t >= SURE_T else 'a weak hint'}).")
        else:
            lines.append(f"Closing at this point has saved {-e:.2f}R a trade compared with holding "
                         f"({'a solid edge' if t <= -SURE_T else 'a weak hint'}).")
    elif plain is not None:
        txt = f"Odds of reaching the target before the stop: {_pct(plain)}, from the distances alone."
        if sid is not None:
            txt += " Not enough of this strategy's past trades stood like this yet for its own figure."
        lines.append(txt)
    elif has_stop and not has_target:
        lines.append("No target set: it runs until the stop or you close it.")
    if age is not None:
        txt = f"Open {_age_text(age)}."
        if left is not None:
            txt += f" This strategy's trades still open at this age took a median {left:.0f} more min to close."
        lines.append(txt)

    return PositionAdvice(
        symbol=sym, direction=p.direction, unrealized_pnl=round(p.unrealized_pnl, 2), has_stop=has_stop,
        severity=severity, headline=headline, detail=" ".join(lines),
        opened_at=getattr(dbp, "opened_at", None) if dbp is not None else None, source=source,
        thesis="unknown", r_multiple=r_now, event_label=ev_label, minutes_to_event=ev_mins,
        events_soon=" · ".join(f"{lbl} ~{m}m" for m, lbl in soft[:3]) or None,
        style="scalp",
        odds=round(odds, 3) if odds is not None else (round(plain, 3) if plain is not None else None),
        odds_from="history" if hist is not None else ("distance" if plain is not None else None),
        odds_n=hist["n"] if hist is not None else None,
        odds_plain=round(plain, 3) if plain is not None else None,
        hold_edge=hist["hold_edge"] if hist is not None else None,
        hold_t=hist["hold_t"] if hist is not None else None,
        minutes_open=round(age, 1) if age is not None else None,
        minutes_left=left,
    )
