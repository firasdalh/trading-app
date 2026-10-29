"""Each symbol's trading hours, LEARNED from the broker's own bar history.

MT5's Python API exposes no session schedule (MQL5's SymbolInfoSessionTrade has no Python twin), and
a hand-kept table of Exness hours would be one more thing to drift. The bars ARE the schedule: no
bars, no trading. Measured on this account (5m bars, 4+ weeks, UTC): EURUSDm stops Fri 21:00 -> Sun
21:05; XAUUSDm, USOILm, STOXX50m, FR40m and JP225m add a daily 21:00-22:00 break, and the indices
stop on Friday at 20:00-20:55; AAPLm trades 10:00-19:50; BTCUSDm never stops.

Hours are kept on a local clock, not on UTC: those boundaries are New York times (the 17:00 rollover,
the 18:00 reopen, the 16:00 US close), but XZNUSDm (LME zinc) keeps LONDON time - it moved from
00:00-18:00 to 01:00-19:00 UTC when UK clocks changed on 2026-10-25. So each symbol's clock is read
from its own history: the zone in which its opens and closes recur at the same wall-clock time week
after week. New York wins a tie (until a US clock change is in the data, New York and UTC look the
same), and the schedule is projected on that clock - when the clocks change, the UTC times move
with them instead of being an hour off until the learned weeks catch up.

A moment of the coming week counts as open when the same local time traded in the majority of the
last three weeks, so a one-off feed gap or last week's holiday is outvoted. Holidays AHEAD are not
known: the times are an estimate, and whether the market is open NOW is the broker's live check.
Read-out only - nothing here gates a trade.
"""
from __future__ import annotations

import threading
import time
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.data.ohlcv_cache import get_ohlcv_cached

log = get_logger("data.market_hours")

NY = ZoneInfo("America/New_York")
# The clocks a symbol's hours can follow, in tie-break order: FX convention first, UTC last.
_ZONES: tuple[tzinfo, ...] = (NY, ZoneInfo("Europe/London"), ZoneInfo("Australia/Sydney"), timezone.utc)
_TF = "5m"
_STEP = timedelta(minutes=5)
_BARS = 6500                       # ~4.5 weeks of a 23h market, ~3 weeks of a 24/7 one
_GAP = timedelta(minutes=15)       # bars this far apart = closed in between; one missing bar is noise
_MIN_RUN = timedelta(minutes=15)   # a predicted close / open must last this long to count
_WEEKS = 3                         # majority vote over this many past weeks
_HORIZON = timedelta(days=8)       # how far ahead to look for the next close / open
_LOOKBACK = timedelta(days=4)      # how far back to look for the start of the closure we are in
_DAILY_BREAK = timedelta(hours=3)  # a closure up to this long is a daily break...
_WEEKEND = timedelta(hours=24)     # ...this long or more is the weekend; between them, overnight
_MODEL_TTL = 3600.0                # the schedule does not change hour to hour


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _floor(t: datetime) -> datetime:
    t = _utc(t)
    return t.replace(second=0, microsecond=0) - timedelta(minutes=t.minute % 5)


def _ceil(t: datetime) -> datetime:
    f = _floor(t)
    return f if f == _utc(t) else f + _STEP


def _zone_for(sessions: list[tuple[datetime, datetime]]) -> tzinfo:
    """The clock a symbol's hours follow: the zone in which the most of its opens and closes recur at
    the same weekday and wall-clock minute in another week. The first open and the last close are the
    edges of the fetched window, not of a session, so they are left out."""
    bounds = [("open", s) for s, _ in sessions[1:]] + [("close", e) for _, e in sessions[:-1]]
    best, best_score = _ZONES[0], -1
    for zone in _ZONES:
        keys = Counter()
        for kind, t in bounds:
            w = t.astimezone(zone)
            keys[(kind, w.weekday(), w.hour * 60 + w.minute)] += 1
        score = sum(n for n in keys.values() if n >= 2)
        if score > best_score:              # strictly better: an earlier zone keeps a tie
            best, best_score = zone, score
    return best


class TradingHours:
    """When a symbol traded (from its 5m bar starts), and what that says about the coming week."""

    def __init__(self, bar_starts: list[datetime]):
        ts = sorted(_utc(t) for t in bar_starts)
        self.sessions: list[tuple[datetime, datetime]] = []
        start = prev = None
        for t in ts:
            if start is None:
                start = t
            elif t - prev >= _GAP:
                self.sessions.append((start, prev + _STEP))
                start = t
            prev = t
        if start is not None:
            self.sessions.append((start, prev + _STEP))
        self._starts = [s for s, _ in self.sessions]
        self.first = ts[0] if ts else None
        self.last = ts[-1] + _STEP if ts else None
        self.zone = _zone_for(self.sessions)

    def _traded(self, u: datetime) -> bool:
        i = bisect_right(self._starts, u) - 1
        return i >= 0 and u < self.sessions[i][1]

    def round_the_clock(self) -> bool:
        """Never closed in a week or more of history (crypto)."""
        return len(self.sessions) == 1 and self.last - self.first >= timedelta(days=7)

    def scheduled_open(self, t: datetime) -> bool | None:
        """Did the same local wall-clock time trade in most of the last weeks? None = no history."""
        if self.first is None:
            return None
        wall = _utc(t).astimezone(self.zone).replace(tzinfo=None)
        votes = []
        for k in range(1, _WEEKS + 1):
            u = (wall - timedelta(weeks=k)).replace(tzinfo=self.zone).astimezone(timezone.utc)
            if self.first <= u < self.last:
                votes.append(self._traded(u))
        if not votes:
            return None
        yes = sum(votes)
        if yes * 2 == len(votes):          # a split (two weeks of history): the latest week decides
            return votes[0]
        return yes * 2 > len(votes)

    def next_change(self, after: datetime, to_open: bool) -> datetime | None:
        """When the schedule next turns open (``to_open``) or closed, for at least _MIN_RUN. None when
        that is beyond _HORIZON or the history can't say."""
        t = _ceil(after)
        end = _utc(after) + _HORIZON
        run = None
        while t < end:
            v = self.scheduled_open(t)
            if v is None:
                return None
            if v == to_open:
                run = run or t
                if t + _STEP - run >= _MIN_RUN:
                    return run
            else:
                run = None
            t += _STEP
        return None

    def closure_start(self, now: datetime) -> datetime | None:
        """Where the scheduled closure ``now`` sits in began. None when the schedule says open."""
        t = _floor(now)
        start = None
        stop = _utc(now) - _LOOKBACK
        while t > stop and self.scheduled_open(t) is False:
            start = t
            t -= _STEP
        return start


def _kind(length: timedelta) -> str:
    if length <= _DAILY_BREAK:
        return "daily_break"
    return "weekend" if length >= _WEEKEND else "overnight"


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def describe(model: TradingHours | None, live: bool | None, now: datetime) -> dict:
    """The read-out for one symbol: open now (``live``, the broker's check), and the next close or
    reopen from the learned schedule.

    ``off_schedule`` = live and schedule disagree right now (a holiday, or trading outside the usual
    hours); the times then come from the NEXT scheduled change, and a closure is ``unscheduled``."""
    now = _utc(now)
    out = {"open": live, "scheduled_open": None, "always_open": False, "closes_at": None,
           "opens_at": None, "closure": None, "off_schedule": False, "learned_days": None}
    if model is None or model.first is None:
        return out
    sched = model.scheduled_open(now)
    out["scheduled_open"] = sched
    out["learned_days"] = (model.last - model.first).days
    out["off_schedule"] = live is not None and sched is not None and live != sched
    if model.round_the_clock():
        out["always_open"] = True
        return out
    if (live if live is not None else sched):
        # Trading outside its usual hours: count from the next scheduled open instead of "now".
        frm = now if sched is not False else model.next_change(now, to_open=True)
        close = model.next_change(frm, to_open=False) if frm else None
        reopen = model.next_change(close, to_open=True) if close else None
        out.update(closes_at=_iso(close), opens_at=_iso(reopen),
                   closure=_kind(reopen - close) if close and reopen else None)
    else:
        reopen = None
        if sched is False:
            reopen = model.next_change(now, to_open=True)
            start = model.closure_start(now)
            out["closure"] = _kind(reopen - start) if reopen and start else None
        elif sched:
            # Closed although it normally trades now (a holiday): the usual reopen after the next
            # scheduled close is the best estimate.
            frm = model.next_change(now, to_open=False)
            reopen = model.next_change(frm, to_open=True) if frm else None
            out["closure"] = "unscheduled"
        out["opens_at"] = _iso(reopen)
    return out


_lock = threading.Lock()
_models: dict[tuple[str, str], tuple[float, TradingHours]] = {}


def hours_for(broker, symbol: str) -> TradingHours | None:
    """The learned schedule for ``symbol``, rebuilt at most every _MODEL_TTL seconds."""
    key = (getattr(broker, "name", "?"), symbol.upper())
    now = time.monotonic()
    with _lock:
        hit = _models.get(key)
    if hit is not None and now - hit[0] < _MODEL_TTL:
        return hit[1]
    try:
        series = get_ohlcv_cached(broker, symbol, _TF, limit=_BARS, ttl=_MODEL_TTL)
    except Exception as exc:  # noqa: BLE001 - advisory: keep the last schedule, or show live status only
        log.warning("market hours: bar history unavailable", extra={"symbol": symbol, "error": str(exc)})
        return hit[1] if hit is not None else None
    model = TradingHours([c.ts for c in series.candles])
    with _lock:
        _models[key] = (now, model)
    return model


def market_hours(broker, symbol: str, now: datetime | None = None) -> dict:
    """Open now? Next close / reopen? See ``describe``."""
    try:
        live = bool(broker.market_open(symbol))
    except Exception:  # noqa: BLE001 - unknown; the schedule alone then answers
        live = None
    return {"symbol": symbol, **describe(hours_for(broker, symbol), live, now or datetime.now(timezone.utc))}


def clear_cache() -> None:
    with _lock:
        _models.clear()
