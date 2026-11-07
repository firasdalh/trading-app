"""Custom strategies: rules the user builds from indicators on any timeframes, which OPEN a trade and
CLOSE it again on their own (the Strategies board, StrategyConfig).

A strategy is a list of pairs, a base timeframe and three rule sets:
  entry_long  - when to BUY;
  entry_short - when to SELL (or, with ``mirror_short``, the BUY rules read the other way round);
  exit_rules  - when to CLOSE, written AGAINST the trade, so one set serves longs and shorts.
Each rule set is a list of GROUPS of conditions: a group holds when ALL its conditions hold, and the
rule set fires when ANY group does ("EMA 10 x 20 AND RSI rising" OR "EMA 20 x 50 AND 1h trend up").
Every condition reads CLOSED bars of its own timeframe only - the forming bar repaints - and an exit
waits for a bar that closed after the entry, so a trade is never closed on the bars that opened it.
It can keep trading hours too (``hours_now``): it then opens trades only inside them, on the user's
clock, while its close rules keep running at any hour on the trades it holds. And a daily target
(``today_result``): once the trades it closed today have made that much, it opens nothing more until
the next day, managing the trades it still holds the same way.

What a strategy cannot do, whatever its rules say:
  - trade a REAL account unless its "Trade the live account" switch is on (RISK.md, 2026-11-04, at the
    user's explicit request; off by default, so a strategy is demo-only). Even then a new real-money
    order needs the user's typed live confirmation, re-typed after every restart - the executor's gate.
  - open without a broker-side stop. Every order carries an ATR stop (pushed out past the spread
    gate's minimum, as the scalp ticket does) and the target when one is set.
  - get round the Risk Manager. Sizing and the per-trade ceiling, exposure, max positions,
    no-stacking, correlation, the daily-loss pause and the spread gate all apply, then the
    executor's kill-switch, drift guard and news gate for automated entries. The cooldowns are the
    strategy's own, set on the board - by default scalp mode's (RISK.md override 2026-11-01): no wait
    between trades on a pair, a 5-min pause in the same direction after a losing one - and never
    under those.
None of the rules was backtested on this book: run a strategy on paper before trusting it.
"""
from __future__ import annotations

import copy
import math
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.indicators import (
    _ema_full, _swing_pivots, adx_series, atr, atr_series, bollinger, pivot_levels, pivot_prices, session_vwap,
    supertrend_series,
)
from app.agents.market_monitor import PAIR_TFS, lean_read
from app.agents.order_blocks import find_order_blocks
from app.core.logging import get_logger
from app.models.db import AgentRun, Position, StrategyConfig, TradeProposalRecord
from app.models.enums import AssetClass, Direction, PositionStatus, ProposalStatus
from app.models.schemas import Candle

log = get_logger("agents.strategy")

TF_SECONDS = {"5s": 5, "10s": 10, "30s": 30, "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900,
              "30m": 1800, "1h": 3600, "4h": 14_400, "1d": 86_400}
TIMEFRAMES = list(TF_SECONDS)
_BARS = 300               # candles read per timeframe (EMA 200 needs the room)
# The engine looks at a pair GRACE seconds after a bar closes on any timeframe its rules read - by
# then the broker's last ticks for that bar are in - and at least every HEARTBEAT seconds anyway, for
# the exits that don't wait for a close (+R, time stop). A 5s strategy is read every 5 s, a 1m-only
# one every minute, each about a second after the close.
_GRACE_S = 1.0
_HEARTBEAT_S = 15.0
_MIN_BARS = 30            # fewer closed bars than this on the base timeframe: wait
_GATE_SLACK = 1.1         # room over the spread gate's minimum stop, as the scalp ticket keeps
_MAX_GROUPS = 6
# 10 until 2026-11-03, then 12 so the ATR and spread filters fit beside a full group of entry rules, then 16
# the same day: the user's BUY group was full again when the stochastic-with-room rule came.
_MAX_CONDITIONS = 16
# Pairs one strategy reads: 10 until 2026-11-07, then 13 at the user's request ("if it won't affect the
# performance"). Measured that day on strategy 1's live pairs: a look costs ~13 ms a pair after a 10s close and
# ~45 ms after a 1m close (the seconds bars built from ticks, then the rules), so a pass over 10 pairs took
# 0.14 s / 0.52 s and 13 take ~0.16 s / ~0.68 s - with ATR(14) read off the bars' memo (_atr14), which took
# ~1.4 ms a pair off every look between closes. About 1.5 s -> 1.8 s of CPU a minute.
_MAX_PAIRS = 13
# The levels and zones the "room" filters read: the windows the chart draws its lines (/market/levels,
# 200 bars) and its order-block zones (/market/orderblocks, 500 bars) from.
_LEVEL_BARS = 200
_ZONE_BARS = 500
_TOUCH_ATR = 0.25                          # a swing this close to a level (in ATR) is another touch
# "Timeframes agree" reads the strip's columns. Each lean is read from 200 bars; the rest is room for a close
# rule to look back for its turn, at most _TURN_STEPS bar closes.
_ALIGN_TFS = PAIR_TFS
_ALIGN_BARS = 500
_TURN_STEPS = 400
_BARS_NEEDED = {"zone_room": _ZONE_BARS, "tf_align": _ALIGN_BARS}   # conditions that read further back than _BARS


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.6g}"


# --------------------------------------------------------------------------- #
#  Bars: one timeframe's closed candles and the series read from them
# --------------------------------------------------------------------------- #

def _rsi_series(closes: list[float], period: int) -> list[float | None]:
    """Wilder's RSI per bar - the smoothing indicators.rsi and the chart use."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if period <= 0 or n <= period:
        return out
    ag = sum(max(closes[i] - closes[i - 1], 0.0) for i in range(1, period + 1)) / period
    al = sum(max(closes[i - 1] - closes[i], 0.0) for i in range(1, period + 1)) / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def _sma_series(vals: list[float | None], p: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    for i in range(p - 1, len(vals)):
        win = vals[i - p + 1:i + 1]
        if all(v is not None for v in win):
            out[i] = sum(win) / p   # type: ignore[arg-type]
    return out


class Bars:
    """A timeframe's CLOSED candles, with each indicator series worked out once per tick."""

    def __init__(self, tf: str, candles: list[Candle]):
        self.tf = tf
        self.c = candles
        self.close = [x.close for x in candles]
        self._memo: dict = {}
        self.leans: dict[datetime, dict | None] = {}   # bar ts -> the strip's lean at its close (_lean)

    def __len__(self) -> int:
        return len(self.c)

    def _m(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def ema(self, p: int) -> list[float | None]:
        return self._m(("ema", p), lambda: _ema_full(self.close, p))

    def rsi(self, p: int) -> list[float | None]:
        return self._m(("rsi", p), lambda: _rsi_series(self.close, p))

    def st(self, p: int, mult: float) -> dict:
        """The SuperTrend's line and direction (1 = green, the line under price; -1 = red, over it)."""
        return self._m(("st", p, mult), lambda: supertrend_series(self.c, p, mult))

    def st_dir(self, p: int, mult: float) -> list[int]:
        return self.st(p, mult)["dir"]

    def vwap(self) -> list[float | None]:
        return self._m(("vwap",), lambda: session_vwap(self.c))

    def adx(self, p: int) -> dict | None:
        return self._m(("adx", p), lambda: adx_series(self.c, p))

    def atr(self, p: int) -> list[float | None]:
        return self._m(("atr", p), lambda: atr_series(self.c, p))

    def macd_hist(self, f: int, s: int, g: int) -> list[float | None]:
        def calc():
            ef, es = _ema_full(self.close, f), _ema_full(self.close, s)
            line = [(a - b) if a is not None and b is not None else None for a, b in zip(ef, es)]
            first = next((i for i, v in enumerate(line) if v is not None), None)
            out: list[float | None] = [None] * len(line)
            if first is None:
                return out
            sig = _ema_full([v for v in line[first:]], g)   # type: ignore[misc]
            for j, sv in enumerate(sig):
                if sv is not None:
                    out[first + j] = line[first + j] - sv    # type: ignore[operator]
            return out
        return self._m(("macd", f, s, g), calc)

    def stoch(self, k: int, smooth: int = 3, d: int = 3) -> tuple[list[float | None], list[float | None]]:
        def calc():
            raw: list[float | None] = [None] * len(self.c)
            for i in range(k - 1, len(self.c)):
                win = self.c[i - k + 1:i + 1]
                hi, lo = max(x.high for x in win), min(x.low for x in win)
                raw[i] = 100.0 * (self.c[i].close - lo) / (hi - lo) if hi > lo else 50.0
            kk = _sma_series(raw, smooth)
            return kk, _sma_series(kk, d)
        return self._m(("stoch", k, smooth, d), calc)

    def swing_points(self) -> tuple[list[Candle], list[float], list[float], float | None]:
        """The last _LEVEL_BARS bars, their swing highs and lows, and their ATR: what levels are made of."""
        def calc():
            cs = self.c[-_LEVEL_BARS:]
            highs, lows = pivot_prices(cs)
            return cs, highs, lows, atr(cs, 14)
        return self._m(("swings",), calc)

    def zones(self) -> list:
        """Every order block of the last _ZONE_BARS bars - not only the 8 strongest the chart draws: a
        few strong demand zones below could push out the supply zone right above an entry."""
        return self._m(("zones",), lambda: find_order_blocks(self.c[-_ZONE_BARS:], max_blocks=1000))


def closed_only(candles: list[Candle], tf: str, now: datetime) -> list[Candle]:
    """Drop the bar still forming: its close, high and low are not final yet."""
    secs = TF_SECONDS.get(tf, 60)
    if candles and _utc(candles[-1].ts) + timedelta(seconds=secs) > now:
        return candles[:-1]
    return candles


def _stale(b: Bars, now: datetime) -> bool:
    """The last closed bar is long gone: a closed session or a stalled feed, not a market to read."""
    if not b.c:
        return True
    secs = TF_SECONDS.get(b.tf, 60)
    return now - (_utc(b.c[-1].ts) + timedelta(seconds=secs)) > timedelta(seconds=3 * secs + 120)


# --------------------------------------------------------------------------- #
#  The conditions
# --------------------------------------------------------------------------- #
# Each is judged FOR a side: s = +1 reads it for a long (the BUY phrasing), s = -1 for a short. An
# exit rule is the same condition judged for the OTHER side of the trade ("SuperTrend green for a
# short" = "SuperTrend turned against my long"), except the exit-only ones, judged for the trade.
# A check returns (True / False, what it read), or (None, why) when it can't read - never a hold.

@dataclass
class Ctx:
    now: datetime
    base_tf: str = "1m"
    spread: float | None = None
    trade: Position | None = None
    bid: float | None = None       # the live quote: where a sell (bid) or a buy (ask) would fill
    ask: float | None = None
    risk: float | None = None      # one R: how far the order's stop would go (_stop_distance)
    bars: dict = field(default_factory=dict)   # every timeframe the rules read, for a rule over several


Result = tuple[bool | None, str]


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    default: float
    lo: float
    hi: float
    step: float = 1.0
    kind: str = "num"     # num | bool


@dataclass(frozen=True)
class Spec:
    type: str
    group: str
    use: str              # entry | exit | both
    long: str             # how it reads in the BUY rules; {key} = a parameter
    short: str            # ... in the SELL rules
    exit: str             # ... in the CLOSE rules, against the trade
    params: tuple[Param, ...]
    fn: Callable[[dict, int, Bars, Ctx], Result]
    tip: str
    suggested: bool = False
    per_side: bool = True  # False: the same reading for both sides (filters)
    tfs: tuple[str, ...] = ()       # the timeframes it can read; empty = any
    default_tf: str | None = None   # the one a new row starts on
    multi_tf: bool = False          # reads the timeframes ticked in its params, not one of its own


def _c_ema_cross(p, s, b, ctx) -> Result:
    f, w = b.ema(int(p["fast"])), b.ema(int(p["slow"]))
    n = len(b)
    if n < 2 or f[-1] is None or w[-1] is None:
        return None, "not enough bars"
    within = max(1, int(p["within"]))
    ago = None
    for i in range(max(1, n - within), n):
        if f[i - 1] is None or w[i - 1] is None:
            continue
        if s * (f[i - 1] - w[i - 1]) <= 0 < s * (f[i] - w[i]):
            ago = n - 1 - i
    holds = s * (f[-1] - w[-1]) > 0
    if holds and ago is not None:
        return True, f"crossed {ago} bar{'' if ago == 1 else 's'} ago"
    if holds:
        return False, f"{'above' if s > 0 else 'below'}, no cross in {within} bars"
    return False, "below" if s > 0 else "above"


def _c_ema_above(p, s, b, ctx) -> Result:
    f, w = b.ema(int(p["fast"]))[-1] if len(b) else None, b.ema(int(p["slow"]))[-1] if len(b) else None
    if f is None or w is None:
        return None, "not enough bars"
    return s * (f - w) > 0, f"{_fmt(f)} vs {_fmt(w)}"


def _c_price_ema(p, s, b, ctx) -> Result:
    e = b.ema(int(p["period"]))
    k = max(1, int(p["bars"]))
    if len(b) < k or e[-k] is None:
        return None, "not enough bars"
    hits = sum(1 for i in range(len(b) - k, len(b)) if e[i] is not None and s * (b.close[i] - e[i]) > 0)
    return hits == k, f"{hits}/{k} closes {'above' if s > 0 else 'below'} {_fmt(e[-1])}"


def _c_rsi_rising(p, s, b, ctx) -> Result:
    r = b.rsi(int(p["period"]))
    k = max(1, int(p["bars"]))
    if len(r) <= k or r[-1] is None or r[-1 - k] is None:
        return None, "not enough bars"
    lim = float(p["limit"])
    moving = s * (r[-1] - r[-1 - k]) > 0
    room = r[-1] <= lim if s > 0 else r[-1] >= 100 - lim
    return moving and room, f"RSI {r[-1]:.0f} ({r[-1] - r[-1 - k]:+.1f})" + ("" if room else ", too stretched")


def _c_rsi_level(p, s, b, ctx) -> Result:
    r = b.rsi(int(p["period"]))[-1] if len(b) else None
    if r is None:
        return None, "not enough bars"
    lv = float(p["level"])
    return (r > lv) if s > 0 else (r < 100 - lv), f"RSI {r:.0f}"


def _c_trend(p, s, b, ctx) -> Result:
    e20, e50 = (b.ema(20)[-1], b.ema(50)[-1]) if len(b) else (None, None)
    if e20 is None or e50 is None:
        return None, "not enough bars"
    c = b.close[-1]
    up, down = e20 > e50 and c > e20, e20 < e50 and c < e20
    return (up if s > 0 else down), "up" if up else "down" if down else "mixed"


def _came_back(b: Bars, n: int, want: int) -> int:
    """How many of the last ``n`` candles did NOT keep going ``want``'s way. Going up (want +1) is a green
    candle or a close higher than the candle before; going down is red, or a lower close. The confirming
    candles of a SuperTrend flip or a structure signal - one that came back is a fake signal."""
    m = len(b.c)
    return sum(1 for j in range(m - n, m)
               if want * (b.c[j].close - b.c[j].open) <= 0 and want * (b.c[j].close - b.c[j - 1].close) <= 0)


def _c_supertrend(p, s, b, ctx) -> Result:
    d = b.st_dir(int(p["period"]), float(p["mult"]))
    if not d or d[-1] == 0:
        return None, "not enough bars"
    want = 1 if s > 0 else -1
    if d[-1] != want:
        return False, "red" if d[-1] < 0 else "green"
    colour = "green" if want > 0 else "red"
    # The bar the line turned this way on (k), and how many bars have closed after it (0 = the last one).
    k = len(d) - 1
    while k > 0 and d[k - 1] == want:
        k -= 1
    ago = len(d) - 1 - k
    bars = lambda n: f"{n} bar{'' if n == 1 else 's'}"
    pos = ctx.trade
    if pos is not None and pos.opened_at is not None:
        # A close rule: the line has to TURN against the trade after it opened. One that was against it
        # already at the entry - another multiplier or timeframe than the entry rules read - is not a
        # turn, or a trade taken there would be closed on its first bar (JP225m, 2026-11-02).
        if _utc(b.c[k].ts) + timedelta(seconds=TF_SECONDS.get(b.tf, 60)) <= _utc(pos.opened_at):
            return False, f"{colour} since {_utc(b.c[k].ts):%H:%M}, before the entry - not a turn"
    # Confirmation by candles: each of the last `hold` candles since the turn moved its way - after a red
    # flip (a long's close rule) a red candle, or a close lower than the candle before: the price kept
    # going down rather than coming back. A candle that came back the other way is a fake flip, and
    # nothing is done on it; if the move then resumes for `hold` candles in a row while the line is
    # still turned, that confirms it.
    hold = int(p["hold"])
    if hold:
        if ago < hold:
            return False, f"flipped {colour} {bars(ago)} ago - waiting for {hold} confirming candle{'' if hold == 1 else 's'}"
        back = _came_back(b, hold, want)
        if back:
            return False, (f"flipped {colour} {bars(ago)} ago - not confirmed: {back} of the last {hold} "
                           f"candle{'' if hold == 1 else 's'} came back {'down' if want > 0 else 'up'}")
    fresh = int(p["fresh"])
    if fresh > 0 and (k == 0 or ago >= fresh):
        return False, f"no flip in {fresh} bars"
    if hold:
        return True, f"flipped {bars(ago)} ago, confirmed by {hold} candle{'' if hold == 1 else 's'} going its way"
    return True, f"flipped {bars(ago)} ago" if fresh > 0 else colour


def _c_macd(p, s, b, ctx) -> Result:
    h = b.macd_hist(int(p["fast"]), int(p["slow"]), int(p["signal"]))
    if len(h) < 2 or h[-1] is None or h[-2] is None:
        return None, "not enough bars"
    ok = s * h[-1] > 0 and (not p.get("rising") or s * (h[-1] - h[-2]) > 0)
    return ok, f"histogram {_fmt(h[-1])}{' building' if s * (h[-1] - h[-2]) > 0 else ' fading'}"


def _c_vwap(p, s, b, ctx) -> Result:
    v = b.vwap()[-1] if len(b) else None
    if v is None:
        return None, "not enough bars"
    return s * (b.close[-1] - v) > 0, f"VWAP {_fmt(v)}"


def _c_adx(p, s, b, ctx) -> Result:
    a = b.adx(int(p["period"]))
    k = int(p["rising"])
    if not a or len(a["adx"]) <= k:
        return None, "not enough bars"
    line, plus, minus = a["adx"], a["plus_di"][-1], a["minus_di"][-1]
    leads = (plus > minus) if s > 0 else (minus > plus)
    who = "+DI" if plus > minus else "-DI"
    ok = round(line[-1], 2) >= float(p["level"]) and leads     # to 2 dp, as indicators.adx reports it
    if not k:
        return ok, f"ADX {line[-1]:.0f}, {who} leads"
    # Rising, never dipping: higher than the bar before on EACH of the last k bars. A rise over the k
    # bars as a whole would pass an ADX that already turned down on the last one - strength fading.
    dips = sum(1 for i in range(1, k + 1) if line[-i] <= line[-i - 1])
    span = "the last bar" if k == 1 else f"the last {k} bars"
    if dips:
        why = f"{line[-1] - line[-2]:+.2f} on the last bar" if k == 1 else f"down on {dips} of {span}"
        return False, f"ADX {line[-1]:.0f} not rising ({why}), {who} leads"
    return ok, f"ADX {line[-1]:.0f} rising ({line[-1] - line[-1 - k]:+.2f} over {span}), {who} leads"


def _c_stoch_cross(p, s, b, ctx) -> Result:
    k, d = b.stoch(int(p["k"]))
    zone = float(p["zone"])
    n = len(k)
    if n < 2 or k[-1] is None:
        return None, "not enough bars"
    for i in range(max(1, n - 2), n):
        if None in (k[i - 1], d[i - 1], k[i], d[i]):
            continue
        crossed = s * (k[i - 1] - d[i - 1]) <= 0 < s * (k[i] - d[i])      # type: ignore[operator]
        turned = (k[i - 1] <= zone) if s > 0 else (k[i - 1] >= 100 - zone)  # type: ignore[operator]
        if crossed and turned:
            return True, f"%K {k[-1]:.0f} turned"
    return False, f"%K {k[-1]:.0f}"


def _c_stoch_room(p, s, b, ctx) -> Result:
    """The stochastic pointing the trade's way with room left (asked for 2026-11-03: "the fast line above
    the slow line, and the stochastic not in a dangerous zone - we have space to go up"): %K on the
    trade's side of %D, and not yet in the zone against it - overbought (>= zone) for a buy, oversold
    (<= 100 - zone) for a sell. No cross needed: it holds for as long as both are true."""
    k, d = b.stoch(int(p["k"]))
    if not k or k[-1] is None or d[-1] is None:
        return None, "not enough bars"
    kk, dd, hi = k[-1], d[-1], float(p["zone"])
    lo = 100 - hi
    side = f"%K {kk:.0f} {'above' if kk > dd else 'under' if kk < dd else 'on'} %D {dd:.0f}"
    if s > 0:
        room = hi - kk
        where = "in the overbought zone" if room <= 0 else f"{room:.0f} below the overbought zone ({hi:.0f})"
    else:
        room = kk - lo
        where = "in the oversold zone" if room <= 0 else f"{room:.0f} above the oversold zone ({lo:.0f})"
    return s * (kk - dd) > 0 and room > 0, f"{side}, {where}"


def _c_volume(p, s, b, ctx) -> Result:
    if len(b) < 22:
        return None, "not enough bars"
    last = b.c[-1]
    avg = sum(x.volume for x in b.c[-21:-1]) / 20
    if avg <= 0:
        return None, "no volume"
    x = last.volume / avg
    return x >= float(p["mult"]) and s * (last.close - last.open) > 0, \
        f"{x:.1f}x average, {'green' if last.close >= last.open else 'red'} bar"


def _c_bb_break(p, s, b, ctx) -> Result:
    bb = bollinger(b.close, 20, float(p["mult"]))
    if not bb:
        return None, "not enough bars"
    edge = bb["upper"] if s > 0 else bb["lower"]
    return s * (b.close[-1] - edge) > 0, f"band {_fmt(edge)}"


def _c_candles(p, s, b, ctx) -> Result:
    k = max(1, int(p["bars"]))
    if len(b) < k:
        return None, "not enough bars"
    n_ok = sum(1 for x in b.c[-k:] if s * (x.close - x.open) > 0)
    return n_ok == k, f"{n_ok}/{k} {'green' if s > 0 else 'red'}"


def _c_clean_candle(p, s, b, ctx) -> Result:
    """The last ``bars`` candles closed the trade's way AND their shape doesn't argue against it: a real
    body (at least ``body``% of the range - not a doji or a spinning top) and no rejection wick against
    the trade longer than ``wick`` x the body (a shooting star on a buy: sellers pushed the high back
    down; a hammer on a sell: buyers pushed the low back up). The reading names the newest bad candle."""
    k = max(1, int(p["bars"]))
    if len(b) < k:
        return None, "not enough bars"
    min_body, max_wick = float(p["body"]) / 100, float(p["wick"])
    for x in reversed(b.c[-k:]):
        rng, body = x.high - x.low, abs(x.close - x.open)
        if rng <= 0 or body < min_body * rng:
            pct = 100 * body / rng if rng > 0 else 0.0
            return False, f"{'doji' if pct < 10 else 'spinning top'} (body {pct:.0f}% of the range)"
        if s * (x.close - x.open) <= 0:
            return False, "red candle" if s > 0 else "green candle"
        against = (x.high - max(x.open, x.close)) if s > 0 else (min(x.open, x.close) - x.low)
        if against > max_wick * body:
            r = round(against / body, 3)          # prices carry float noise: 2x must read as 2x
            name = ("shooting star" if s > 0 else "hammer") if r >= 2 else (
                "upper-wick rejection" if s > 0 else "lower-wick rejection")
            return False, f"{name} (wick {r:.1f}x the body)"
    x = b.c[-1]
    body = abs(x.close - x.open)
    against = (x.high - max(x.open, x.close)) if s > 0 else (min(x.open, x.close) - x.low)
    return True, f"{'green' if s > 0 else 'red'}, body {100 * body / (x.high - x.low):.0f}%, wick {against / body:.1f}x"


def _c_structure(p, s, b, ctx) -> Result:
    """Swing structure from the last two confirmed swing highs and lows (market_structure's strict
    pivots, ``swing`` bars each side - the monitor's Structure row at 3). For a buy: a higher high,
    then a higher low, the higher low being the newest swing; for a sell: a lower low, then a lower
    high. A swing is known only ``swing`` bars after it, so nothing here repaints.

    As a close rule it is read for the other side: a short closes at the higher low that follows a
    higher high. That swing must be confirmed after the trade opened - a short taken into a structure
    already rising is closed by the next higher low, not by the ones it was opened against."""
    w = int(p["swing"])
    if len(b) < 2 * w + 3:
        return None, "not enough bars"
    highs, lows = b._m(("pivots", w), lambda: _swing_pivots(b.c, w, w, 0))
    if len(highs) < 2 or len(lows) < 2:
        return False, "not enough swings yet"
    c = b.c
    h1, h2, l1, l2 = c[highs[-2]].high, c[highs[-1]].high, c[lows[-2]].low, c[lows[-1]].low
    seen = f"{'HH' if h2 > h1 else 'LH'} {_fmt(h2)} · {'HL' if l2 > l1 else 'LL'} {_fmt(l2)}"
    if s > 0:
        ok, last = h2 > h1 and l2 > l1 and lows[-1] > highs[-1], lows[-1]        # the HL, after the HH
    else:
        ok, last = l2 < l1 and h2 < h1 and highs[-1] > lows[-1], highs[-1]       # the LH, after the LL
    if not ok:
        return False, seen
    confirmed = last + w                      # the bar that made the swing a swing
    ago = len(b) - 1 - confirmed
    within = int(p["within"])
    if within and ago >= within:
        return False, f"{seen}, confirmed {ago} bars ago"
    pos = ctx.trade
    if pos is not None and pos.opened_at is not None and \
            _utc(c[confirmed].ts) + timedelta(seconds=TF_SECONDS.get(b.tf, 60)) <= _utc(pos.opened_at):
        return False, f"{seen}, but confirmed before the entry"
    # Confirming candles, as for SuperTrend: after the signal is known, the last `hold` candles kept
    # going its way (up after an HH + HL, down after an LL + LH). One that came back is a fake signal.
    hold = int(p["hold"])
    if hold:
        if ago < hold:
            return False, f"{seen}, confirmed {ago} bars ago - waiting for {hold} confirming candle{'' if hold == 1 else 's'}"
        back = _came_back(b, hold, 1 if s > 0 else -1)
        if back:
            return False, (f"{seen} - not confirmed: {back} of the last {hold} candle{'' if hold == 1 else 's'} "
                           f"came back {'down' if s > 0 else 'up'}")
        return True, f"{seen}, confirmed {ago} bars ago + {hold} candle{'' if hold == 1 else 's'} going its way"
    return True, f"{seen}, confirmed {ago} bar{'' if ago == 1 else 's'} ago"


def _c_atr(p, s, b, ctx) -> Result:
    """The market moving enough: the ATR at least ``mult`` x its own ``avg``-bar average on EACH of the
    last ``bars`` bars (asked for 2026-11-03: "if the ATR is not strong enough in the last one, two or X
    candles, don't enter"). It is measured against the pair's own average, not a price, so one setting
    reads alike on BTC and on EURUSD."""
    a = b.atr(int(p["period"]))
    n, k, mult = int(p["avg"]), int(p["bars"]), float(p["mult"])
    first = len(a) - k - n + 1                  # the oldest ATR the last k averages take in
    if first < 0 or a[first] is None:
        return None, "not enough bars"
    ratios = []                                 # the last bar's first
    for j in range(len(a) - 1, len(a) - 1 - k, -1):
        avg = sum(a[j - n + 1:j + 1]) / n       # type: ignore[arg-type]
        if avg <= 0:
            return None, "no movement to measure"
        ratios.append(a[j] / avg)               # type: ignore[operator]
    now = f"ATR {_fmt(a[-1])} = {ratios[0]:.2f}x its {n}-bar average"
    weak = sum(1 for r in ratios if r < mult)
    if k == 1:
        return not weak, now
    if weak:
        return False, f"{now}; under {mult:g}x on {weak} of the last {k} bars"
    return True, f"{now}; at least {min(ratios):.2f}x on each of the last {k} bars"


def _atr14(b: Bars) -> float | None:
    """``atr(b.c, 14)`` - the same number, rounded the same - off the bars' memoized series. The spread rule
    and one R read it on every look (the spread rule once a side per group it is in, watch-only ones too):
    recomputed from all 500 bars each time it cost ~1.4 ms a pair a look (measured 2026-11-07)."""
    s = b.atr(14)
    return round(s[-1], 6) if s and s[-1] is not None else None


def _c_spread(p, s, b, ctx) -> Result:
    a = _atr14(b)
    if ctx.spread is None or not a:
        return None, "no spread / ATR"
    pct = 100 * ctx.spread / a
    return pct <= float(p["max_pct"]), f"spread {pct:.0f}% of ATR"


_SESSIONS = (("asia", 0, 9), ("london", 7, 16), ("newyork", 12, 21))   # UTC hours


def _c_session(p, s, b, ctx) -> Result:
    h = ctx.now.hour
    on = [name for name, a, z in _SESSIONS if p.get(name) and a <= h < z]
    picked = [name for name, _a, _z in _SESSIONS if p.get(name)]
    if not picked:
        return None, "no session picked"
    return bool(on), (f"{' + '.join(on)} open" if on else f"{h:02d}:00 UTC, outside")


def _c_sr_room(p, s, b, ctx) -> Result:
    """Room between the entry and the nearest level ahead: resistance above a buy, support under a
    sell. The levels are the chart's swing pivots of the timeframe, measured from the live price, so
    a level price has already broken is behind it. Touches = swing points within _TOUCH_ATR of it."""
    price = ctx.ask if s > 0 else ctx.bid
    if not price or not ctx.risk:
        return None, "no quote / stop distance"
    if len(b) < 20:
        return None, "not enough bars"
    cs, highs, lows, a = b.swing_points()
    want, side = ("resistance", "above") if s > 0 else ("support", "below")
    need, tol = int(p["touches"]), _TOUCH_ATR * (a or 0.0)
    best = None
    for lv in pivot_levels(cs, price, per_side=10):
        if lv["kind"] != want:
            continue
        touches = max(1, sum(1 for x in highs + lows if abs(x - lv["price"]) <= tol))
        if touches >= need and (best is None or s * (lv["price"] - price) < best[0]):
            best = (s * (lv["price"] - price), lv["price"], touches)
    if best is None:
        return True, f"no {want} touched {need}+ times {side}"
    gap_r = best[0] / ctx.risk
    return gap_r >= float(p["room"]), \
        f"{want} {_fmt(best[1])} ({best[2]} touch{'' if best[2] == 1 else 'es'}) {gap_r:.1f}R {side}"


def _c_zone_room(p, s, b, ctx) -> Result:
    """No buy inside a supply zone or with one closer than the room above it (a sell: demand)."""
    price = ctx.ask if s > 0 else ctx.bid
    if not price or not ctx.risk:
        return None, "no quote / stop distance"
    if len(b) < 60:
        return None, "not enough bars"
    want, name, side = ("bearish", "supply", "above") if s > 0 else ("bullish", "demand", "below")
    zones = [z for z in b.zones() if z.kind == want and z.strength >= float(p["strength"])]
    for z in zones:
        if z.contains(price):
            return False, f"inside {name} {_fmt(z.bottom)}-{_fmt(z.top)} (strength {z.strength})"
    ahead = [z for z in zones if (z.bottom > price if s > 0 else z.top < price)]
    if not ahead:
        return True, f"no {name} zone {side}"
    z = min(ahead, key=lambda x: (x.bottom - price) if s > 0 else (price - x.top))
    gap_r = ((z.bottom - price) if s > 0 else (price - z.top)) / ctx.risk
    return gap_r >= float(p["room"]), \
        f"{name} {_fmt(z.bottom)}-{_fmt(z.top)} (strength {z.strength}) {gap_r:.1f}R {side}"


def _ticked(p: dict) -> list[str]:
    """The timeframes a "Timeframes agree" rule reads, fastest first."""
    return [tf for tf in _ALIGN_TFS if p.get(tf)]


def _lean(b: Bars, i: int) -> dict | None:
    """The strip's lean of ``b``'s timeframe at the close of bar ``i`` (market_monitor.lean_read), worked out
    once per bar: load_bars hands the leans on to the timeframe's next read."""
    ts = b.c[i].ts
    if ts not in b.leans:
        b.leans[ts] = lean_read(b.c[:i + 1], b.tf)
    return b.leans[ts]


def _now_leans(picked: list[str], bars: dict) -> dict[str, dict | None]:
    """Each of these timeframes' lean at its last closed bar; None = no candles to read."""
    return {tf: _lean(bars[tf], len(bars[tf]) - 1) if tf in bars and len(bars[tf]) else None for tf in picked}


def _arrow(lean: dict) -> str:
    """A lean as the strip prints it: ▲5/7, ▼4/7, or ≈3·2 (up · down) when neither side leads."""
    if lean["side"] > 0:
        return f"▲{lean['up']}/{lean['of']}"
    if lean["side"] < 0:
        return f"▼{lean['down']}/{lean['of']}"
    return f"≈{lean['up']}·{lean['down']}"


def _held_since(picked: list[str], bars: dict, want: int, need: int, opened: datetime) -> bool:
    """Whether "``need`` of ``picked`` lean ``want``" has held without a break since ``opened``, the moment of
    the entry included. It steps back through the ticked timeframes' bar closes, newest first, to the last
    moment it didn't hold: none since the entry = it already held when the trade was opened. Out of bars (or
    of _TURN_STEPS) first, it counts as a turn, as for SuperTrend when its run is older than its bars."""
    idx = {tf: len(bars[tf]) - 1 for tf in picked if tf in bars and len(bars[tf])}

    def closed(tf: str) -> datetime:
        return _utc(bars[tf].c[idx[tf]].ts) + timedelta(seconds=TF_SECONDS.get(tf, 60))

    for _ in range(_TURN_STEPS):
        tf = max(idx, key=closed)
        if closed(tf) <= opened:
            return True             # every bar here closed by the entry: the state the trade was opened in
        idx[tf] -= 1                # the moment just before that bar closed - still after the entry
        if idx[tf] < 0 or _lean(bars[tf], idx[tf]) is None:
            return False
        if sum(1 for t, i in idx.items() if (v := _lean(bars[t], i)) and v["side"] == want) < need:
            return False            # it didn't hold then: it turned after the entry
    return False


def _c_tf_align(p, s, b, ctx) -> Result:
    """The Timeframes strip agreeing (asked for 2026-11-04: "when the timeframes I pick are aligned together,
    open a trade, and close it on its own when they flip against it"). Each ticked timeframe leans the trade's
    way by the strip's own verdict - a lead of 2+ of its 7 readings (market_monitor.lean_of) - read on closed
    bars like every rule here. ``need`` of them, 0 = every one ticked.

    As a close rule it is judged for the other side: the ticked timeframes leaning against the trade. And it
    has to turn that way after the trade opened. If it already held then - a timeframe the entry rules didn't
    read was against it - it is not a turn, or the trade would be closed on its first bar (as SuperTrend's)."""
    picked = _ticked(p)
    if not picked:
        return None, "no timeframe ticked"
    want, way = (1, "up") if s > 0 else (-1, "down")
    need = min(int(p["need"]) or len(picked), len(picked))
    leans = _now_leans(picked, ctx.bars)
    agree = sum(1 for v in leans.values() if v and v["side"] == want)
    unread = [tf for tf, v in leans.items() if v is None]
    seen = " · ".join(f"{tf} {_arrow(v) if v else 'no read'}" for tf, v in leans.items())
    if agree < need:
        if agree + len(unread) >= need:
            return None, f"{seen} - can't read {', '.join(unread)} yet"
        return False, f"{seen} - {agree} of {len(picked)} lean {way}, needs {'all' if need == len(picked) else need}"
    pos = ctx.trade
    if pos is not None and pos.opened_at is not None and _held_since(picked, ctx.bars, want, need, _utc(pos.opened_at)):
        return False, f"{seen} - {way} already when the trade opened, not a turn"
    return True, f"{seen} - {agree} of {len(picked)} lean {way}"


def _align_label(p: dict, where: str) -> str:
    """How a "Timeframes agree" rule reads in the log: "All of 10S · 30S · 1M lean down on the Timeframes strip"."""
    picked = [tf.upper() for tf in _ticked(p)]
    if not picked:
        return "Timeframes agree - none ticked"
    need = int(p["need"])
    who = picked[0] if len(picked) == 1 else \
        f"{'All' if not need or need >= len(picked) else f'At least {need}'} of {' · '.join(picked)}"
    if where == "exit":
        return f"{who} turned against the trade on the Timeframes strip"
    return f"{who} {'leans' if len(picked) == 1 else 'lean'} {'up' if where == 'long' else 'down'} on the Timeframes strip"


def _trade_r(pos: Position | None) -> float | None:
    if pos is None:
        return None
    risk = pos.initial_risk_amount or pos.risk_amount
    return (pos.unrealized_pnl or 0.0) / risk if risk and risk > 0 else None


def _c_rsi_extreme(p, s, b, ctx) -> Result:
    r = b.rsi(int(p["period"]))[-1] if len(b) else None
    if r is None:
        return None, "not enough bars"
    lv = float(p["level"])
    return (r >= lv) if s > 0 else (r <= 100 - lv), f"RSI {r:.0f}"


def _c_time_stop(p, s, b, ctx) -> Result:
    pos = ctx.trade
    if pos is None or pos.opened_at is None:
        return None, "no trade"
    bars = (ctx.now - _utc(pos.opened_at)).total_seconds() / TF_SECONDS.get(b.tf, 60)
    r = _trade_r(pos)
    stuck = bars >= float(p["bars"]) and (r is None or r < float(p["min_r"]))
    return stuck, f"{bars:.0f} bars in, {'—' if r is None else f'{r:+.2f}R'}"


def _c_profit_r(p, s, b, ctx) -> Result:
    r = _trade_r(ctx.trade)
    if r is None:
        return None, "no trade"
    return r >= float(p["r"]), f"{r:+.2f}R"


def _ema_params(fast: int, slow: int) -> tuple[Param, ...]:
    return (Param("fast", "fast", fast, 2, 400), Param("slow", "slow", slow, 3, 400))


_ST = (Param("period", "ATR", 10, 3, 50), Param("mult", "×", 2.7, 0.5, 10, 0.1))

CATALOG: dict[str, Spec] = {s.type: s for s in (
    Spec("ema_cross", "Trend", "both",
         "EMA {fast} crosses above EMA {slow} (in the last {within} bars)",
         "EMA {fast} crosses below EMA {slow} (in the last {within} bars)",
         "EMA {fast} crosses back through EMA {slow}, against the trade (last {within} bars)",
         _ema_params(10, 20) + (Param("within", "bars", 3, 1, 50),), _c_ema_cross,
         "The fast average crossing the slow one: a push starting. 'In the last N bars' keeps a cross "
         "that happened a moment before the other conditions lined up; it must still hold now."),
    Spec("ema_above", "Trend", "both",
         "EMA {fast} is above EMA {slow}", "EMA {fast} is below EMA {slow}",
         "EMA {fast} is on the wrong side of EMA {slow}",
         _ema_params(20, 50), _c_ema_above,
         "The averages stacked the trade's way - the state, not the moment of the cross."),
    Spec("price_ema", "Trend", "both",
         "The last {bars} closes are above EMA {period}", "The last {bars} closes are below EMA {period}",
         "{bars} closes in a row beyond EMA {period}, against the trade",
         (Param("period", "EMA", 10, 2, 400), Param("bars", "closes", 2, 1, 10)), _c_price_ema,
         "Closes, not wicks: '2 closes below EMA 10' is the classic confirmation that a push is over."),
    Spec("trend", "Trend", "both",
         "The trend is up (EMA 20 over EMA 50, close above EMA 20)",
         "The trend is down (EMA 20 under EMA 50, close below EMA 20)",
         "The trend turned against the trade",
         (), _c_trend,
         "A quick trend read for a higher timeframe: pick 1m, 5m or 1h and only trade its way."),
    Spec("tf_align", "Trend", "both",
         "{5s}{10s}{30s}{1m}{3m}{5m}{15m}{1h}{4h}{1d} lean up on the Timeframes strip · how many of the ticked ones: "
         "{need} (0 = all of them)",
         "{5s}{10s}{30s}{1m}{3m}{5m}{15m}{1h}{4h}{1d} lean down on the Timeframes strip · how many of the ticked ones: "
         "{need} (0 = all of them)",
         "{5s}{10s}{30s}{1m}{3m}{5m}{15m}{1h}{4h}{1d} turn against the trade on the Timeframes strip after the entry - down "
         "against a long, up against a short · how many of the ticked ones: {need} (0 = all of them)",
         # The fast four ticked to start. The 3m (2026-11-05) and the 5s (2026-11-06) start off, so a rule
         # saved before them reads as it did.
         tuple(Param(tf, tf.upper(), 1 if tf in ("10s", "30s", "1m", "5m") else 0, 0, 1, 1, "tf") for tf in _ALIGN_TFS)
         + (Param("need", "how many", 0, 0, len(_ALIGN_TFS)),), _c_tf_align,
         "The Timeframes strip above the chart, as a rule. Tick the timeframes it reads: each leans the way the "
         "strip shows it - ▲ up when 2+ more of its 7 readings point up than down (trend, SuperTrend, MACD, RSI, "
         "ADX·DI, structure, buyers / sellers), ▼ down the other way, ≈ neither. Read on closed bars like every rule "
         "here, so it can be up to a bar of each timeframe behind the strip, which also counts the bar still "
         "forming. As a close rule: the ticked timeframes turning against the trade after it opened - leaning "
         "against it already at the entry is not a turn, and ≈ is not against. How many: 0 = every ticked one, "
         "1 = any one of them.", suggested=True, multi_tf=True),
    Spec("supertrend", "Trend", "both",
         "SuperTrend ({period}, {mult}) is green · fresh flip in the last {fresh} bars (0 = not needed) · "
         "confirmed by {hold} candle(s) after the flip closing green or higher (0 = not needed)",
         "SuperTrend ({period}, {mult}) is red · fresh flip in the last {fresh} bars (0 = not needed) · "
         "confirmed by {hold} candle(s) after the flip closing red or lower (0 = not needed)",
         "SuperTrend ({period}, {mult}) turns against the trade, confirmed by {hold} candle(s) after the flip "
         "that keep going its way - red or a lower close for a long, green or a higher close for a short "
         "(0 = close on the turn)",
         _ST + (Param("fresh", "bars", 0, 0, 50), Param("hold", "candles", 0, 0, 20)), _c_supertrend,
         "The ATR trailing line the chart draws. Green = buyers in control. As a close rule it is the "
         "classic exit: the line flipping against you on a faster timeframe. Confirming candles: after "
         "the flip, the last 1 or 2 candles must keep going its way - after a red flip each one red, or "
         "closing lower than the one before. A candle that comes back is a fake flip and nothing is done, at "
         "the cost of a later exit when it is real."),
    Spec("adx", "Trend", "entry",
         "ADX {period} is at least {level}, buyers (+DI) leading · rising on each of the last {rising} bar(s) "
         "(0 = not needed)",
         "ADX {period} is at least {level}, sellers (-DI) leading · rising on each of the last {rising} bar(s) "
         "(0 = not needed)",
         "",
         (Param("period", "period", 14, 5, 50), Param("level", "level", 20, 5, 60),
          Param("rising", "bars", 0, 0, 20)), _c_adx,
         "Trend STRENGTH. Under ~20 the market is chopping: most crossover signals there are noise. "
         "The single best filter to add to an EMA-cross strategy. Rising: higher than the bar before on "
         "each of the last N bars - the trend getting stronger, not running out of steam (ADX 30 and "
         "falling often is). ADX is strength, not direction: it rises in a falling market too, so a SELL "
         "wants it rising as well.", suggested=True),
    Spec("rsi_rising", "Momentum", "both",
         "RSI {period} rising over {bars} bar(s), and under {limit}",
         "RSI {period} falling over {bars} bar(s), and over {~limit}",
         "RSI {period} turning against the trade (over {bars} bar(s))",
         (Param("period", "RSI", 7, 2, 50), Param("bars", "bars", 1, 1, 10), Param("limit", "limit", 80, 50, 100)),
         _c_rsi_rising,
         "Momentum building the trade's way. The limit stops a buy at an already stretched RSI - the "
         "top of a burst."),
    Spec("rsi_level", "Momentum", "both",
         "RSI {period} above {level}", "RSI {period} below {~level}",
         "RSI {period} crosses {level} against the trade",
         (Param("period", "RSI", 14, 2, 50), Param("level", "level", 50, 1, 99)), _c_rsi_level,
         "RSI over 50 = buyers have the edge on that timeframe. As a close rule: RSI back under 50."),
    Spec("macd", "Momentum", "both",
         "MACD ({fast}, {slow}, {signal}) histogram above 0 - and rising: {rising}",
         "MACD ({fast}, {slow}, {signal}) histogram below 0 - and falling: {rising}",
         "MACD ({fast}, {slow}, {signal}) histogram flips against the trade",
         (Param("fast", "fast", 12, 2, 100), Param("slow", "slow", 26, 3, 200), Param("signal", "signal", 9, 2, 50),
          Param("rising", "building", 1, 0, 1, 1, "bool")), _c_macd,
         "Momentum and whether it is still building. 3 / 10 / 16 is the fast scalping setting.",
         suggested=True),
    Spec("stoch_cross", "Momentum", "entry",
         "Stochastic ({k}, 3, 3) turns up out of the low zone: %K crosses up through %D from under {zone} "
         "(in the last 2 bars)",
         "Stochastic ({k}, 3, 3) turns down out of the high zone: %K crosses down through %D from over {~zone} "
         "(in the last 2 bars)",
         "",
         (Param("k", "%K", 5, 3, 50), Param("zone", "zone", 30, 5, 50)), _c_stoch_cross,
         "A pullback ending: the fast oscillator turning up out of the low zone inside an uptrend. It holds "
         "only on the bar of that turn (or the one after). Pair it with a trend condition - alone it fades "
         "every strong move. For 'pointing our way, not overbought' use Stochastic with room.", suggested=True),
    Spec("stoch_room", "Momentum", "entry",
         "Stochastic ({k}, 3, 3) with room to go up: the fast line %K is above the slow line %D, and %K is "
         "under {zone} - not overbought",
         "Stochastic ({k}, 3, 3) with room to go down: the fast line %K is below the slow line %D, and %K is "
         "over {~zone} - not oversold",
         "",
         (Param("k", "%K", 5, 3, 50), Param("zone", "zone", 80, 50, 95)), _c_stoch_room,
         "The stochastic already pointing the trade's way, with room left before the zone where a move "
         "tends to turn back: over 80 is overbought (dangerous for a buy), under 20 oversold (dangerous for "
         "a sell). No cross needed - it holds for as long as %K stays on the trade's side of %D and out of "
         "that zone. The chart's stochastic is the same (5, 3, 3).", suggested=True),
    Spec("vwap", "Volume & volatility", "both",
         "Close above the session VWAP", "Close below the session VWAP", "Close back through the VWAP, against the trade",
         (), _c_vwap,
         "The day's average price: above it buyers have the session. A close back through it is a "
         "common scalp exit.", suggested=True),
    Spec("volume", "Volume & volatility", "entry",
         "The last bar is green on at least {mult}x the 20-bar average volume",
         "The last bar is red on at least {mult}x the 20-bar average volume",
         "",
         (Param("mult", "x", 1.5, 1.0, 10, 0.1),), _c_volume,
         "Participation: a break on busy bars is the one that runs; on quiet bars it is the one that "
         "fails. (MT5 gives tick counts, not traded volume.)", suggested=True),
    Spec("bb_break", "Volume & volatility", "both",
         "Close above the upper Bollinger band (20, {mult})", "Close below the lower Bollinger band (20, {mult})",
         "Close beyond the far Bollinger band (20, {mult}), against the trade",
         (Param("mult", "σ", 2.0, 0.5, 4, 0.1),), _c_bb_break,
         "A momentum breakout out of the band."),
    Spec("candles", "Price action", "both",
         "The last {bars} candle(s) closed green", "The last {bars} candle(s) closed red",
         "{bars} candle(s) in a row closed against the trade",
         (Param("bars", "candles", 1, 1, 10),), _c_candles,
         "The bars themselves agreeing."),
    Spec("clean_candle", "Price action", "both",
         "The last {bars} candle(s) closed green with a real body (at least {body}% of its range) and an upper "
         "wick no longer than {wick}x the body - no doji, spinning top or shooting star",
         "The last {bars} candle(s) closed red with a real body (at least {body}% of its range) and a lower "
         "wick no longer than {wick}x the body - no doji, spinning top or hammer",
         "{bars} strong candle(s) against the trade - a real body the other way, no wick rejecting it",
         (Param("bars", "candles", 1, 1, 5), Param("body", "%", 30, 5, 90, 5), Param("wick", "x", 1.0, 0.2, 5, 0.1)),
         _c_clean_candle,
         "The candle agreeing, not just its colour. A doji or spinning top is indecision; a shooting star (long "
         "upper wick) is sellers rejecting a push up; a hammer (long lower wick) is buyers rejecting a push down. "
         "Each fails the rule on the side it argues against. A bigger body % or a smaller wick x is stricter.",
         suggested=True),
    Spec("structure", "Price action", "both",
         "Structure up: a higher high, then a higher low (swings of {swing} bars; confirmed in the last {within} bars, "
         "0 = any) · then {hold} candle(s) closing green or higher (0 = not needed)",
         "Structure down: a lower low, then a lower high (swings of {swing} bars; confirmed in the last {within} bars, "
         "0 = any) · then {hold} candle(s) closing red or lower (0 = not needed)",
         "Structure turns against the trade - a short closes at the higher low after a higher high, a long at the "
         "lower high after a lower low (swings of {swing} bars, made after the entry), confirmed by {hold} candle(s) "
         "going its way - green or higher against a short, red or lower against a long (0 = at once)",
         (Param("swing", "bars", 3, 2, 10), Param("within", "bars", 0, 0, 50), Param("hold", "candles", 0, 0, 20)),
         _c_structure,
         "Market structure from the swing highs and lows, as the monitor's Structure row reads it (3 bars each "
         "side). As a close rule: you are short and price makes a higher high, then a higher low - the downtrend "
         "is over, so it closes as soon as that higher low is confirmed. Fewer swing bars = sooner, but noisier. "
         "Confirming candles: then wait for 1 or 2 candles that keep going that way - one that comes back is a "
         "fake signal and nothing is done."),
    Spec("sr_room", "Levels & zones", "entry",
         "No resistance touched {touches}+ times within {room}R above the entry",
         "No support touched {touches}+ times within {room}R below the entry",
         "",
         (Param("room", "R", 1.5, 0.1, 10, 0.1), Param("touches", "touches", 2, 1, 5)), _c_sr_room,
         "Keeps a buy from running into a higher-timeframe ceiling, and a sell into a floor. The levels are the "
         "swing highs and lows the chart's Levels menu draws for 1h / 4h / 1d; touches = the swings at that "
         "price (1 = every line, 2+ = levels price turned at more than once). Room is in R, the trade's own "
         "risk: ask for at least your target, so it comes before the level. A level already broken is behind "
         "the price and doesn't count.",
         suggested=True, tfs=("1h", "4h", "1d"), default_tf="1h"),
    Spec("zone_room", "Levels & zones", "entry",
         "Not inside a supply zone (strength {strength}+), and none within {room}R above",
         "Not inside a demand zone (strength {strength}+), and none within {room}R below",
         "",
         (Param("room", "R", 1.0, 0, 10, 0.1), Param("strength", "strength", 60, 0, 100, 5)), _c_zone_room,
         "Keeps a buy out of supply - the area a sharp fall started from, where sellers may still be waiting - "
         "and a sell out of demand. The zones are the chart's order blocks with their 0-100 strength; every "
         "zone at that strength counts, not only the 8 strongest the chart draws. Room 0 = only when inside one.",
         suggested=True, tfs=("5m", "15m", "30m", "1h", "4h", "1d"), default_tf="1h"),
    Spec("atr", "Filters", "entry",
         "ATR {period} is at least {mult}x its {avg}-bar average on each of the last {bars} bar(s)",
         "ATR {period} is at least {mult}x its {avg}-bar average on each of the last {bars} bar(s)", "",
         (Param("period", "ATR", 14, 2, 50), Param("mult", "x its average", 1.0, 0.3, 5, 0.05),
          Param("avg", "average over", 50, 10, 200), Param("bars", "bars", 1, 1, 20)), _c_atr,
         "The market moving enough. A quiet one moves too little to pay the spread, and its signals go "
         "nowhere. Measured against the pair's own recent ATR, so one setting fits every pair: 1x = at "
         "least its usual range. On each of the last N bars, so a single spike doesn't open the gate.",
         suggested=True, per_side=False),
    Spec("spread", "Filters", "entry",
         "The spread is at most {max_pct}% of the ATR", "The spread is at most {max_pct}% of the ATR", "",
         (Param("max_pct", "%", 25, 1, 200),), _c_spread,
         "A scalp pays the spread up front: past about a quarter of the ATR it eats the target.",
         suggested=True, per_side=False),
    Spec("session", "Filters", "entry",
         "Only during: Asia {asia} · London {london} · New York {newyork} (UTC)",
         "Only during: Asia {asia} · London {london} · New York {newyork} (UTC)", "",
         (Param("asia", "Asia", 0, 0, 1, 1, "bool"), Param("london", "London", 1, 0, 1, 1, "bool"),
          Param("newyork", "New York", 1, 0, 1, 1, "bool")), _c_session,
         "Breakouts in thin hours fail far more often: trade the busy sessions.", suggested=True,
         per_side=False),
    Spec("rsi_extreme", "Take profit", "exit", "", "",
         "RSI {period} stretched in the trade's favour: over {level} on a buy, under {~level} on a sell",
         (Param("period", "RSI", 7, 2, 50), Param("level", "level", 80, 55, 99)), _c_rsi_extreme,
         "Bank it when the move is stretched: past 80 a fast RSI usually pauses or snaps back."),
    Spec("profit_r", "Take profit", "exit", "", "", "The trade reaches +{r}R",
         (Param("r", "R", 1.5, 0.2, 20, 0.1),), _c_profit_r,
         "A soft target in R (multiples of the risk at entry), before the broker's own target."),
    Spec("time_stop", "Take profit", "exit", "", "", "{bars} bars in and still under {min_r}R",
         (Param("bars", "bars", 10, 1, 500), Param("min_r", "R", 0.3, -5, 10, 0.1)), _c_time_stop,
         "A scalp is a bet on a quick move: a time stop gets out of the ones that don't come."),
)}


def catalog() -> list[dict]:
    """The conditions as the board shows them."""
    return [
        {"type": s.type, "group": s.group, "use": s.use, "long": s.long, "short": s.short, "exit": s.exit,
         "tip": s.tip, "suggested": s.suggested, "tfs": list(s.tfs) or None, "default_tf": s.default_tf,
         "multi_tf": s.multi_tf,
         "params": [{"key": q.key, "label": q.label, "default": q.default, "min": q.lo, "max": q.hi,
                     "step": q.step, "kind": q.kind} for q in s.params]}
        for s in CATALOG.values()
    ]


def clean_params(spec: Spec, raw: dict | None) -> dict:
    """A condition's parameters, each one present and inside its bounds."""
    raw = raw or {}
    out: dict = {}
    for q in spec.params:
        try:
            v = float(raw.get(q.key, q.default))
        except (TypeError, ValueError):
            v = q.default
        v = min(q.hi, max(q.lo, v))
        out[q.key] = int(round(v)) if q.step >= 1 else round(v, 4)
    return out


def label(cond: dict, where: str) -> str:
    """How a condition reads in the BUY ("long"), SELL ("short") or CLOSE ("exit") rules."""
    spec = CATALOG.get(cond.get("type", ""))
    if spec is None:
        return cond.get("type", "?")
    p = clean_params(spec, cond.get("params"))
    if spec.multi_tf:
        return _align_label(p, where)
    text = getattr(spec, where) or spec.long
    shown = {k: ("on" if v == 1 else "off") if any(q.key == k and q.kind == "bool" for q in spec.params) else v
             for k, v in p.items()}
    # {~key}: the parameter read from the other side of the 0-100 scale (a SELL's 20 for a BUY's 80).
    for k, v in p.items():
        text = text.replace("{~" + k + "}", f"{100 - v:g}")
    try:
        text = text.format(**shown)
    except (KeyError, IndexError, ValueError):
        pass
    return f"{text} · {cond.get('tf', '?')}"


# --------------------------------------------------------------------------- #
#  Reading the rules
# --------------------------------------------------------------------------- #

@dataclass
class Rules:
    """What evaluation needs from a strategy - a saved row or a draft from the board."""
    timeframe: str = "1m"
    sides: str = "both"
    entry_long: list = field(default_factory=list)
    entry_short: list = field(default_factory=list)
    mirror_short: bool = True
    exit_rules: list = field(default_factory=list)
    stop_atr: float = 1.5          # sizes one R, which the room filters measure in
    hours: dict | None = None      # its trading hours (hours_now); None = around the clock
    stop: dict | None = None       # a SuperTrend stop (st_stop); None = stop_atr x ATR
    off: dict = field(default_factory=dict)   # rule set -> the places of its groups switched off on the board

    @classmethod
    def of(cls, src) -> "Rules":
        return cls(timeframe=src.timeframe, sides=src.sides, entry_long=list(src.entry_long or []),
                   entry_short=list(src.entry_short or []), mirror_short=bool(src.mirror_short),
                   exit_rules=list(src.exit_rules or []), stop_atr=float(src.stop_atr or 1.5),
                   hours=src.hours or None, stop=getattr(src, "stop", None) or None,
                   off=getattr(src, "off_groups", None) or {})

    def buy_rules(self) -> list:
        return self.entry_long

    def sell_rules(self) -> list:
        return self.entry_long if self.mirror_short else self.entry_short

    def off_of(self, name: str) -> set[int]:
        return set(self.off.get(name) or ())

    def buy_off(self) -> set[int]:
        return self.off_of("entry_long")

    def sell_off(self) -> set[int]:
        """A mirrored SELL is the BUY groups read the other way round, so it has their switches."""
        return self.off_of("entry_long" if self.mirror_short else "entry_short")

    def _conditions(self):
        for groups in (self.entry_long, self.entry_short, self.exit_rules):
            for g in groups:
                yield from g

    def tfs(self) -> set[str]:
        out = {self.timeframe}
        for c in self._conditions():
            out.update(_reads(c, self.timeframe))
        if self.stop:
            out.add(self.stop.get("tf") or self.timeframe)    # the stop follows a line on it
        return {t for t in out if t in TF_SECONDS}

    def sizes(self) -> dict[str, int]:
        """Bars to read per timeframe: _BARS, or more where a condition looks further back."""
        out = {t: _BARS for t in self.tfs()}
        for c in self._conditions():
            for tf in _reads(c, self.timeframe):
                if tf in out:
                    out[tf] = max(out[tf], _BARS_NEEDED.get(c.get("type", ""), _BARS))
        return out


def _reads(cond: dict, base: str) -> list[str]:
    """The timeframes a condition reads: its own, or the ones a rule over several ticks."""
    spec = CATALOG.get(cond.get("type", ""))
    if spec is not None and spec.multi_tf:
        return _ticked(clean_params(spec, cond.get("params")))
    return [cond.get("tf") or base]


def evaluate(groups: list, s: int, data: dict[str, Bars], ctx: Ctx, where: str, off: set[int] = frozenset()) -> dict:
    """A rule set read now: each group and each of its conditions, and whether any group holds.

    ``where`` is "long" / "short" (entries, judged for that side) or "exit" (judged against a trade
    on side ``s``: each condition for the other side, the exit-only ones for the trade itself).
    ``off``: the groups switched off on the board. They are read like the others, so the board can show
    them, and marked "off" - but they never fire the rule set."""
    out = {"ok": False, "groups": []}
    ctx = replace(ctx, bars=data)
    for gi, g in enumerate(groups):
        rows, all_ok = [], bool(g)
        for cond in g:
            spec = CATALOG.get(cond.get("type", ""))
            # A rule over several timeframes reads the ones it ticks from ctx.bars; it waits on the base one.
            multi = spec is not None and spec.multi_tf
            tf = ctx.base_tf if multi else cond.get("tf") or ctx.base_tf
            b = data.get(tf)
            ok: bool | None
            if spec is None:
                ok, detail = None, "unknown condition"
            elif b is None or not len(b):
                ok, detail = None, f"no {tf} candles"
            elif (where == "exit" and spec.use != "exit" and ctx.trade is not None and ctx.trade.opened_at
                  and _utc(b.c[-1].ts) + timedelta(seconds=TF_SECONDS.get(tf, 60)) <= _utc(ctx.trade.opened_at)):
                ok, detail = None, f"waiting for a {tf} bar after the entry"
            else:
                judged = s if (where != "exit" or spec.use == "exit") else -s
                try:
                    ok, detail = spec.fn(clean_params(spec, cond.get("params")), judged, b, ctx)
                except Exception as exc:  # noqa: BLE001 - one bad reading never fires a rule
                    ok, detail = None, f"error: {exc}"
            picked = _reads(cond, ctx.base_tf) if multi else [tf]
            row = {"type": cond.get("type"), "tf": "·".join(picked), "ok": ok, "detail": detail, "label": label(cond, where)}
            if multi:
                # Each ticked timeframe's lean, for the board and the widget to draw the way the strip does.
                row["leans"] = [{"tf": t, **(v or {"side": None})} for t, v in _now_leans(picked, data).items()]
            rows.append(row)
            all_ok = all_ok and ok is True
        out["groups"].append({"ok": all_ok, "conditions": rows, **({"off": True} if gi in off else {})})
        if all_ok and gi not in off and not out["ok"]:
            out["ok"], out["group"] = True, gi
    return out


def bar_mark(tf: str, now: datetime) -> int:
    """Which bar of ``tf`` closed last, read GRACE seconds late - it changes once per bar, a second
    after each close. (Bars start on whole multiples of their length, as the broker's do.)"""
    return int((now.timestamp() - _GRACE_S) // TF_SECONDS.get(tf, 60))


# Each timeframe's closed bars, kept until the next one closes: closed bars don't change, so a 1m
# series is read from the broker once a minute however often the 5s rules beside it are checked.
_BAR_CACHE: dict[tuple[str, str, str], tuple[int, int, Bars]] = {}
_bar_lock = threading.Lock()


def load_bars(broker, symbol: str, tfs: set[str], now: datetime,
              sizes: dict[str, int] | None = None) -> dict[str, Bars]:
    """Each timeframe's closed candles, fresh from the broker once per closed bar (``sizes``: how many,
    per timeframe, _BARS if not given). A timeframe that can't be read is left out (its conditions
    then read 'no candles' and never hold)."""
    from app.data.ohlcv_cache import get_ohlcv_cached

    out: dict[str, Bars] = {}
    for tf in sorted(tfs, key=lambda t: TF_SECONDS.get(t, 60)):
        n = (sizes or {}).get(tf, _BARS)
        key, mark = (getattr(broker, "name", "?"), symbol, tf), bar_mark(tf, now)
        with _bar_lock:
            hit = _BAR_CACHE.get(key)
        if hit is not None and hit[0] == mark and hit[1] >= n:
            out[tf] = hit[2]
            continue
        try:
            series = get_ohlcv_cached(broker, symbol, tf, n, ttl=0.0)   # a bar just closed: read it now
            bars = Bars(tf, closed_only(list(series.candles), tf, now))
        except Exception as exc:  # noqa: BLE001
            log.warning("strategy candles unavailable", extra={"symbol": symbol, "tf": tf, "error": str(exc)})
            continue
        if hit is not None and bars.c and hit[2].c and bars.c[0].ts >= hit[2].c[0].ts:
            # Closed bars don't change, so the leans read at their closes (_lean) still hold. Not when the new
            # read reaches further back: its first bars have more history behind them now.
            bars.leans = {ts: v for ts, v in hit[2].leans.items() if ts >= bars.c[0].ts}
        with _bar_lock:
            _BAR_CACHE[key] = (mark, n, bars)
        out[tf] = bars
    return out


def _quote(broker, symbol: str) -> dict | None:
    """Bid, ask, spread, digits and the broker's minimum stop distance for the order."""
    try:
        spec = broker.scalp_spec(symbol)
    except Exception:  # noqa: BLE001
        spec = None
    if spec:
        return spec
    try:
        mid = broker.get_quote(symbol).price
    except Exception:  # noqa: BLE001
        return None
    try:
        sp = broker.spread(symbol) or 0.0
    except Exception:  # noqa: BLE001
        sp = 0.0
    return {"bid": mid - sp / 2, "ask": mid + sp / 2, "spread": sp, "digits": 6, "stops_level": 0.0}


def _stop_distance(session: Session, stop_atr: float, base: Bars | None, q: dict | None) -> float | None:
    """One R: how far the order's stop goes - stop_atr x ATR(14) of the base timeframe, pushed out past
    the spread gate's minimum and the broker's stop level, as the scalp ticket does. None without a
    quote or an ATR."""
    a = _atr14(base) if base is not None else None
    if not q or not a or a <= 0:
        return None
    return max(stop_atr * a, _stop_floor(session, q))


def _stop_floor(session: Session, q: dict) -> float:
    """The closest a new trade's stop may sit: past the spread gate's minimum and the broker's stop level."""
    from app.core.state import get_or_create_risk_config

    risk_cfg = get_or_create_risk_config(session)
    gate = risk_cfg.max_spread_r_fraction if risk_cfg.spread_gate_enabled else None
    spread, stops = float(q.get("spread") or 0.0), float(q.get("stops_level") or 0.0)
    floor = spread + stops
    if gate and gate > 0:
        floor = max(floor, (spread / gate + spread / 2) * _GATE_SLACK)
    return floor


def st_stop(stop: dict | None, data: dict[str, Bars], side: int) -> dict | None:
    """Where a SuperTrend stop sits for a ``side`` trade now (asked for 2026-11-03: "the stop follows the
    SuperTrend line of the timeframe I choose - not exactly on it, give it room for a false break"):
    ``buffer`` x ATR beyond the ``tf`` line, read on closed bars. None when the line isn't on the trade's
    side of the price (red over a buy, green under a sell), or can't be read yet."""
    if not stop:
        return None
    b = data.get(stop.get("tf") or "")
    period, mult = int(stop.get("period") or 10), float(stop.get("mult") or 2.7)
    if b is None or len(b) < period + 2:
        return None
    st, a = b.st(period, mult), b.atr(period)[-1]
    line = st["line"][-1]
    if st["dir"][-1] != side or line is None or a is None:
        return None
    return {"level": line - side * float(stop.get("buffer") or 0.0) * a, "line": line, "atr": a,
            "tf": stop.get("tf")}


def _side_risk(session: Session, rules: Rules, data: dict[str, Bars], q: dict | None, side: int) -> float | None:
    """One R for a ``side`` trade: to its SuperTrend stop when it has one (never closer than the floor a new
    trade's stop keeps), else stop_atr x ATR. None = no stop to place."""
    if not rules.stop:
        return _stop_distance(session, rules.stop_atr, data.get(rules.timeframe), q)
    want = st_stop(rules.stop, data, side)
    if not q or want is None:
        return None
    dist = side * (float(q["ask"] if side > 0 else q["bid"]) - want["level"])
    return max(dist, _stop_floor(session, q)) if dist > 0 else None


def _quote_ctx(q: dict | None) -> dict:
    """The quote's part of a Ctx."""
    if not q:
        return {"spread": None, "bid": None, "ask": None}
    return {"spread": float(q["spread"]), "bid": float(q["bid"]), "ask": float(q["ask"])}


def _norm(sym: str | None) -> str:
    return "".join(ch for ch in (sym or "").upper() if ch.isalnum())


def strategy_source(cfg: StrategyConfig) -> str:
    return f"strategy:{cfg.id}"


def _open_positions(session: Session, symbol: str) -> list[Position]:
    rows = session.scalars(select(Position).where(Position.status == PositionStatus.OPEN.value)).all()
    return [p for p in rows if _norm(p.symbol) == _norm(symbol)]


def _trades_today(session: Session, cfg: StrategyConfig, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.scalars(select(Position).where(Position.source == strategy_source(cfg))).all()
    return sum(1 for p in rows if p.opened_at is not None and _utc(p.opened_at) >= start)


def today_result(session: Session, cfg: StrategyConfig, now: datetime) -> dict:
    """What its trades made today - the UTC day, like its daily cap and the journal's "today": the
    realized P&L of the ones it closed, stops and targets included once the broker's figure is in, and
    when that first reached its daily target (asked for 2026-11-03: "if today we made $300, we stop
    trading for today"). Reached stays reached for the day, even if a trade still open then closes at a
    loss; a target raised later is read afresh. ``hit_at`` is None with no target set."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.scalars(select(Position).where(
        Position.source == strategy_source(cfg), Position.status == PositionStatus.CLOSED.value,
        Position.realized_pnl.is_not(None), Position.closed_at >= start)).all()
    done = sorted((p for p in rows if p.closed_at and _utc(p.closed_at) >= start), key=lambda p: _utc(p.closed_at))
    total, hit = 0.0, None
    for p in done:
        total += p.realized_pnl
        if hit is None and cfg.daily_target and round(total, 2) >= cfg.daily_target:
            hit = _utc(p.closed_at)
    return {"pnl": round(total, 2), "trades": len(done), "hit_at": hit}


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _log_target(session: Session, cfg: StrategyConfig, day: dict, now: datetime) -> None:
    """The feed's "target reached" row: once a day - again only if a raised target is reached too."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seen = session.scalars(select(AgentRun).where(AgentRun.agent == "strategy", AgentRun.event == "target_reached",
                                                  AgentRun.created_at >= start)).all()
    if any((r.detail or {}).get("strategy_id") == cfg.id and (r.detail or {}).get("target") == cfg.daily_target
           for r in seen):
        return
    _log(session, cfg, None, "target_reached", pnl=day["pnl"], target=cfg.daily_target, trades=day["trades"],
         at=day["hit_at"].isoformat())
    log.warning("strategy reached its daily target", extra={"strategy": cfg.id, "pnl": day["pnl"]})


def _cooldown_left(session: Session, cfg: StrategyConfig, symbol: str, now: datetime) -> int:
    """Minutes left of the strategy's own re-entry wait after the last trade on the pair closed - any
    trade, its own or the user's, a win or a loss (0 = free to enter). Read before the entry rules so a
    pair that is cooling down says so on the board, instead of being refused by the Risk Manager on
    every bar the rules hold. The Risk Manager still checks the same wait on every order."""
    until = cooldown_until(session, cfg, symbol)
    left = (until - now).total_seconds() if until else 0
    return math.ceil(left / 60) if left > 0 else 0


def cooldown_until(session: Session, cfg: StrategyConfig, symbol: str) -> datetime | None:
    """When that re-entry wait ends on the pair (``_cooldown_left``), in the past once it has; None =
    no wait set, or no trade on the pair has closed yet. The board and the widget show it, so a pair
    that is waiting reads as waiting rather than "GO" (asked for 2026-11-03)."""
    from app.risk.service import last_pair_close_at, strategy_cooldowns

    wait = strategy_cooldowns(cfg.cooldown_minutes, cfg.loss_cooldown_minutes)["override_cooldown_minutes"]
    last = last_pair_close_at(session, symbol) if wait else None
    return last + timedelta(minutes=wait) if last else None


def _hhmm(text: str) -> int:
    """'09:30' -> minutes after midnight."""
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def hours_now(hours: dict | None, now: datetime) -> dict | None:
    """Where ``now`` falls in a strategy's trading hours (asked for 2026-11-03: "run from 9 am, stop at
    12"): whether it may open a trade now, and in how many seconds that changes. The hours run from
    ``start`` up to ``end`` (past midnight when end comes first) on the strategy's clock - ``tz``, the
    user's browser zone, or this machine's own when there is none. None = no hours: around the clock."""
    if not hours or not hours.get("start") or not hours.get("end"):
        return None
    start, end = _hhmm(hours["start"]), _hhmm(hours["end"])
    if start == end:
        return None
    try:
        zone = ZoneInfo(hours["tz"]) if hours.get("tz") else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    local = _utc(now).astimezone(zone)
    m = local.hour * 60 + local.minute
    inside = start <= m < end if start < end else (m >= start or m < end)
    edge = end if inside else start
    return {"open": inside, "start": hours["start"], "end": hours["end"], "tz": hours.get("tz") or local.tzname(),
            "now": local.strftime("%H:%M"), "next_s": (edge - m) % 1440 * 60 - local.second}


def _log(session: Session, cfg: StrategyConfig, symbol: str | None, event: str, **detail) -> None:
    session.add(AgentRun(agent="strategy", symbol=symbol, event=event,
                         detail={"strategy_id": cfg.id, "name": cfg.name, **detail}))


# --------------------------------------------------------------------------- #
#  The engine
# --------------------------------------------------------------------------- #

def _open(session: Session, cfg: StrategyConfig, symbol: str, ac: str, side: str, ev: dict,
          base: Bars, ctx: Ctx, broker, why: list[str], data: dict[str, Bars] | None = None) -> dict:
    """Size and send the order the rules asked for, through the Risk Manager and the executor."""
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.models.schemas import TradeProposal
    from app.risk.service import assess, scalp_risk_ceiling, strategy_cooldowns

    rules = Rules.of(cfg)
    q = _quote(broker, symbol)
    s = 1 if side == "long" else -1
    dist = _side_risk(session, rules, data or {cfg.timeframe: base}, q, s)
    if q is not None and dist is None and rules.stop:
        return {"symbol": symbol, "skipped": f"its {rules.stop.get('tf')} SuperTrend is against a {side} - "
                                             "no line to put the stop on"}
    if q is None or dist is None:
        return {"symbol": symbol, "skipped": "no quote / ATR to set the stop from"}
    entry = float(q["ask"] if s > 0 else q["bid"])
    digits = int(q.get("digits") or 6)
    stop = round(entry - s * dist, digits)
    target = round(entry + s * cfg.target_r * dist, digits) if cfg.target_r else None
    prop = TradeProposal(
        symbol=symbol, asset_class=AssetClass(ac), timeframe=cfg.timeframe,
        direction=Direction.LONG if s > 0 else Direction.SHORT, entry=round(entry, digits),
        stop_loss=stop, take_profit=target, confidence=1.0,
        rationale=f"Strategy '{cfg.name}': " + "; ".join(why), strategy="strategy",
    )
    # The strategy's own cooldowns (set on the board, never under scalp mode's 0 / 5 min). They go into
    # the order's reasoning too, so the executor's last check holds it to the same ones.
    waits = strategy_cooldowns(cfg.cooldown_minutes, cfg.loss_cooldown_minutes)
    # Up to 6% a trade, scalp mode's ceiling (RISK.md override 2026-11-02).
    dec = assess(session, prop, override_risk_fraction=cfg.risk_pct, **waits,
                 **scalp_risk_ceiling(strategy_source(cfg), None))
    if not dec.approved or not dec.approved_qty:
        _log(session, cfg, symbol, "refused", side=side, reason=dec.reason, rules=why)
        return {"symbol": symbol, "refused": dec.reason}
    record = TradeProposalRecord(
        symbol=symbol, asset_class=ac, timeframe=cfg.timeframe, direction=prop.direction.value,
        entry=prop.entry, stop_loss=prop.stop_loss, take_profit=prop.take_profit, confidence=0.0,
        rationale=prop.rationale, source=strategy_source(cfg),
        reasoning={"strategy_id": cfg.id, "name": cfg.name, "group": ev.get("group"), "rules": why,
                   "cooldowns": {"pair": waits["override_cooldown_minutes"],
                                 "loss": waits["override_loss_cooldown_minutes"]}},
        risk_decision=dec.decision.value, risk_reason=dec.reason,
        approved_qty=dec.approved_qty, risk_amount=dec.risk_amount, status=ProposalStatus.APPROVED.value,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    try:
        execute_proposal(session, record)    # kill-switch, drift guard, news gate, final risk check
    except ExecutionBlocked as exc:
        # Expired, not pending: a robot's order that could not go out is not one for the user to
        # approve later at a price it was never sized for.
        record.status, record.risk_reason = ProposalStatus.EXPIRED.value, f"not sent: {exc}"
        _log(session, cfg, symbol, "blocked", side=side, reason=str(exc), rules=why)
        session.commit()
        return {"symbol": symbol, "blocked": str(exc)}
    session.refresh(record)
    if record.status != ProposalStatus.EXECUTED.value:
        record.status = ProposalStatus.EXPIRED.value
        _log(session, cfg, symbol, "not_filled", side=side, status=record.status, rules=why)
        session.commit()
        return {"symbol": symbol, "skipped": f"order not filled ({record.status})"}
    _log(session, cfg, symbol, "opened", side=side, lots=record.approved_qty, entry=record.entry,
         stop=record.stop_loss, target=record.take_profit, rules=why,
         **({"stop_on": f"{rules.stop.get('tf')} SuperTrend"} if rules.stop else {}))
    session.commit()
    log.warning("strategy opened a trade", extra={"strategy": cfg.id, "symbol": symbol, "side": side})
    return {"symbol": symbol, "opened": side, "lots": record.approved_qty}


def _trail(session: Session, cfg: StrategyConfig, rules: Rules, pos: Position, data: dict[str, Bars], broker,
           q: dict | None) -> dict | None:
    """Move ``pos``'s stop to its SuperTrend stop (``st_stop``) when that is tighter: only ever toward the
    trade, so it only takes risk off. Left alone when the line has turned against the trade (its close
    rules and its stop deal with that), when the level would sit at or past the price or inside the
    broker's minimum distance, and when another trade shares the pair and side - the broker moves the
    stop of every ticket on that side."""
    from app.risk.service import _risk_per_lot

    s = 1 if pos.direction == "long" else -1
    want = st_stop(rules.stop, data, s)
    if want is None or not q:
        return None
    level = round(want["level"], int(q.get("digits") or 6))
    if pos.stop_loss and s * (level - pos.stop_loss) <= 0:
        return None
    price = float(q["bid"] if s > 0 else q["ask"])
    if s * (price - level) <= float(q.get("stops_level") or 0.0):
        return None
    if any(p.id != pos.id and p.direction == pos.direction for p in _open_positions(session, pos.symbol)):
        return None
    was = pos.stop_loss
    res = broker.set_sl_tp(pos.symbol, stop_loss=level, take_profit=None, direction=pos.direction)
    if res.status.value in ("error", "rejected"):
        _log(session, cfg, pos.symbol, "stop_move_failed", side=pos.direction, reason=res.error, to=level)
        return None
    pos.stop_loss = level
    loss = s * (pos.entry_price - level)
    pos.risk_amount = round((_risk_per_lot(broker, pos.symbol, pos.entry_price, level) or 0.0) * (pos.qty or 0.0), 2) \
        if loss > 0 else 0.0
    _log(session, cfg, pos.symbol, "stop_moved", side=pos.direction, frm=was, to=level, line=want["line"],
         tf=want["tf"], yours=pos.source != strategy_source(cfg))
    return {"symbol": pos.symbol, "stop": level}


def _close(session: Session, cfg: StrategyConfig, pos: Position, broker, why: list[str]) -> dict | None:
    from app.execution.monitor import _close_position

    try:
        price = broker.get_quote(pos.symbol).price
    except Exception:  # noqa: BLE001
        price = pos.last_price or pos.entry_price
    reason = f"strategy '{cfg.name}': " + "; ".join(why)
    if not _close_position(session, pos, broker, price, reason[:200]):
        _log(session, cfg, pos.symbol, "close_failed", side=pos.direction, rules=why)
        session.commit()
        return {"symbol": pos.symbol, "close_failed": True}
    _log(session, cfg, pos.symbol, "closed", side=pos.direction, pnl=pos.realized_pnl, rules=why,
         manual=pos.source != strategy_source(cfg))
    session.commit()
    log.warning("strategy closed a trade", extra={"strategy": cfg.id, "symbol": pos.symbol, "why": why})
    return {"symbol": pos.symbol, "closed": pos.direction, "pnl": pos.realized_pnl}


def _note(state: dict, symbol: str, text: str, now: datetime) -> dict:
    """What the pair is doing, for the board - stamped only when it changes, so a quiet strategy
    doesn't rewrite its row every few seconds."""
    st = state.setdefault(symbol, {})
    if st.get("note") != text:
        st["note"], st["at"] = text, now.isoformat()
    return {"symbol": symbol, "note": text}


# Watch-only groups (asked 2026-11-06): a BUY / SELL group switched off on the board opens nothing, but it is
# read on every look and a "signal" is logged the moment all of it starts to hold - the desk announces it.
# (strategy, symbol, side, group) -> (held on the last look, when it last signalled). In memory: after a
# restart a group already holding counts as seen, so a reload never replays a burst of signals.
_WATCH: dict[tuple[int, str, str, int], tuple[bool, float]] = {}
# At most one signal per pair, side and group in this long: a group flickering in and out on 5s bars would
# otherwise ring every few seconds.
_WATCH_GAP_S = 300.0


def _watch(session: Session, cfg: StrategyConfig, rules: Rules, symbol: str, data: dict[str, Bars], ctx: Ctx,
           q: dict | None, now: datetime) -> None:
    """Read the switched-off BUY / SELL groups; log a "signal" when one starts to hold. Never trades.

    Read whatever holds a trade back (a trade open, a cooldown, the daily cap or target, the kill-switch, an
    unconfirmed live account): it opens nothing. Only the market has to be live and the strategy inside its
    hours - outside them it is asleep, signals included."""
    if not (rules.buy_off() or rules.sell_off()):
        return
    base = data.get(rules.timeframe)
    if base is None or _stale(base, now):
        return
    hrs = hours_now(rules.hours, now)
    if hrs and not hrs["open"]:
        return
    for side, s, groups, off in (("long", 1, rules.buy_rules(), rules.buy_off()),
                                 ("short", -1, rules.sell_rules(), rules.sell_off())):
        if rules.sides not in ("both", side):
            continue
        side_ctx = None
        for gi in sorted(off):
            if gi >= len(groups) or not groups[gi]:
                continue
            if side_ctx is None:
                side_ctx = replace(ctx, risk=_side_risk(session, rules, data, q, s)) if rules.stop else ctx
            ev = evaluate([groups[gi]], s, data, side_ctx, side)
            key = (cfg.id, symbol, side, gi)
            was = _WATCH.get(key)
            last = was[1] if was else 0.0
            if ev["ok"] and was is not None and not was[0] and now.timestamp() - last >= _WATCH_GAP_S:
                last = now.timestamp()
                _log(session, cfg, symbol, "signal", side=side, group=gi + 1, watch=True,
                     price=side_ctx.ask if s > 0 else side_ctx.bid,
                     rules=[c["label"] for c in ev["groups"][0]["conditions"]])
            _WATCH[key] = (ev["ok"], last)


def _run_pair(session: Session, cfg: StrategyConfig, pair: dict, state: dict, now: datetime,
              day: dict | None = None) -> dict:
    """One pair: manage the trades it holds, then look for an entry. ``day``: ``today_result``, when it
    has a daily target."""
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings, kill_switch_active, live_execution_allowed

    symbol, ac = pair.get("symbol") or "", pair.get("asset_class") or "forex"
    settings = get_or_create_settings(session)
    broker = get_broker_for(AssetClass(ac), settings.broker_map)
    real = not getattr(broker, "is_paper", False)
    if real and not cfg.live:
        return _note(state, symbol, "live account - its 'Trade the live account' switch is off (demo only)", now)
    rules = Rules.of(cfg)
    data = load_bars(broker, symbol, rules.tfs(), now, rules.sizes())
    base = data.get(cfg.timeframe)
    if base is None or len(base) < _MIN_BARS:
        return _note(state, symbol, f"reading the {cfg.timeframe} candles", now)
    q = _quote(broker, symbol)
    ctx = Ctx(now=now, base_tf=cfg.timeframe, risk=_stop_distance(session, cfg.stop_atr, base, q), **_quote_ctx(q))
    own = strategy_source(cfg)
    try:
        _watch(session, cfg, rules, symbol, data, ctx, q, now)
    except Exception as exc:  # noqa: BLE001 - a watch-only read never gets in the way of the trades
        log.warning("strategy watch failed", extra={"strategy": cfg.id, "symbol": symbol, "error": str(exc)})

    # 1) The trades it manages: its own, and the user's on this pair when asked.
    mine = [p for p in _open_positions(session, symbol)
            if p.source == own or (cfg.manage_manual and (p.source or "manual") == "manual")]
    for pos in mine:
        if not rules.exit_rules:
            break
        s = 1 if pos.direction == "long" else -1
        ev = evaluate(rules.exit_rules, s, data, replace(ctx, trade=pos), "exit", off=rules.off_of("exit_rules"))
        if ev["ok"]:
            why = [c["label"] for c in ev["groups"][ev["group"]]["conditions"]]
            done = _close(session, cfg, pos, broker, why)
            if done:
                state.setdefault(symbol, {})["acted_bar"] = base.c[-1].ts.isoformat()
                return done
    if rules.stop:
        # The stops of the trades it manages follow the SuperTrend line: its own, and - with "close my own
        # trades" on - the user's on this pair (asked 2026-11-03). Only ever toward the trade.
        for pos in mine:
            _trail(session, cfg, rules, pos, data, broker, q)
    if any(p.source == own for p in mine):
        return _note(state, symbol, "in a trade - watching the close rules", now)
    if mine:
        return _note(state, symbol, "your trade on this pair - its close rules"
                                    f"{' and SuperTrend stop' if rules.stop else ''} are on it; no new entry", now)
    if _open_positions(session, symbol):
        return _note(state, symbol, "you have a trade open on this pair - no new entry", now)

    # 2) A new entry: at most one per closed bar of the base timeframe.
    st = state.setdefault(symbol, {})
    bar = base.c[-1].ts.isoformat()
    if st.get("acted_bar") == bar:
        return {"symbol": symbol, "note": st.get("note")}
    if kill_switch_active(session):
        return _note(state, symbol, "kill-switch on - no new trades", now)
    if real and not live_execution_allowed(settings):
        # A new real-money order needs the typed live confirmation (the executor refuses it otherwise); the
        # trades it holds were managed above.
        return _note(state, symbol, "live account - confirm LIVE trading (typed, again after every restart) "
                                    "to let it open trades", now)
    if day and day["hit_at"]:
        # Done for the day. As outside its hours, the trades it holds were managed above.
        return _note(state, symbol, f"daily target reached - {_usd(day['pnl'])} today, target ${cfg.daily_target:,.2f}: "
                                    "no new trades until 00:00 UTC", now)
    hrs = hours_now(cfg.hours, now)
    if hrs and not hrs["open"]:
        # Only the entries wait: the trades it holds were managed above, at any hour.
        return _note(state, symbol, f"outside its hours - no new trades until {hrs['start']}", now)
    if _stale(base, now) or not broker.market_open(symbol):
        return _note(state, symbol, "market closed / no fresh candles", now)
    if _trades_today(session, cfg, now) >= cfg.max_trades_day:
        return _note(state, symbol, f"{cfg.max_trades_day} trades today - the daily cap", now)
    left = _cooldown_left(session, cfg, symbol, now)
    if left:
        return _note(state, symbol, f"cooldown after the last close on this pair - {left} min left", now)
    for side, s, groups, off in (("long", 1, rules.buy_rules(), rules.buy_off()),
                                 ("short", -1, rules.sell_rules(), rules.sell_off())):
        if cfg.sides not in ("both", side) or not groups:
            continue
        # With a SuperTrend stop each side has its own R: to the line on its side of the price.
        side_ctx = replace(ctx, risk=_side_risk(session, rules, data, q, s)) if rules.stop else ctx
        # The switched-off groups were read by _watch: here they are empty, so they never hold and the
        # others keep their places.
        ev = evaluate([[] if gi in off else g for gi, g in enumerate(groups)], s, data, side_ctx, side, off=off)
        if ev["ok"]:
            st["acted_bar"] = bar        # one try per bar, filled or refused
            why = [c["label"] for c in ev["groups"][ev["group"]]["conditions"]]
            res = _open(session, cfg, symbol, pair.get("asset_class") or "forex", side, ev, base, side_ctx, broker,
                        why, data=data)
            st["note"] = (f"opened {side}" if res.get("opened")
                          else f"{side} signal refused: {res.get('refused') or res.get('blocked') or res.get('skipped')}")
            st["at"] = now.isoformat()
            return res
    return _note(state, symbol, "watching - no entry rule holds", now)


def run_strategy(session: Session, cfg: StrategyConfig, now: datetime | None = None,
                 pairs: list[dict] | None = None) -> list[dict]:
    """One look at the strategy's pairs (``pairs``: only these - the ones a bar just closed for)."""
    now = now or datetime.now(timezone.utc)
    state = copy.deepcopy(cfg.state or {})
    # Its daily target, read once a pass for all its pairs: reached = no new entry on any of them today.
    day = today_result(session, cfg, now) if cfg.daily_target else None
    if day and day["hit_at"]:
        _log_target(session, cfg, day, now)
    results = []
    for pair in (cfg.pairs or []) if pairs is None else pairs:
        try:
            results.append(_run_pair(session, cfg, pair, state, now, day))
        except Exception as exc:  # noqa: BLE001 - one bad pair never stops the others
            log.warning("strategy pair failed", extra={"strategy": cfg.id, "symbol": pair.get("symbol"), "error": str(exc)})
            results.append(_note(state, pair.get("symbol") or "?", f"error: {exc}", now))
    if state != (cfg.state or {}):
        cfg.state = state             # a new object, so the JSON column is written
    acted = [r for r in results if r.get("opened") or r.get("closed")]
    cfg.last_run_at = now
    cfg.last_result = "; ".join(
        f"{r['symbol']} {'opened ' + r['opened'] if r.get('opened') else 'closed ' + r['closed']}" for r in acted
    ) or cfg.last_result
    session.commit()
    return results


# (strategy, symbol) -> the bar marks and the time of its last look.
_LAST_LOOK: dict[tuple[int, str], tuple[tuple, float]] = {}


def strategy_tick(session: Session, now: datetime | None = None) -> dict:
    """Scheduler entrypoint, every second. A running strategy's pair is looked at when a bar has just
    closed on any timeframe its rules read (GRACE seconds after the close), and HEARTBEAT seconds
    after its last look otherwise - in between there is nothing new to read."""
    now = now or datetime.now(timezone.utc)
    running = session.scalars(select(StrategyConfig).where(StrategyConfig.enabled.is_(True))).all()
    out, alive = {}, set()
    for cfg in running:
        marks = tuple(sorted((tf, bar_mark(tf, now)) for tf in Rules.of(cfg).tfs()))
        due = []
        for pair in cfg.pairs or []:
            key = (cfg.id, pair.get("symbol") or "")
            alive.add(key)
            last = _LAST_LOOK.get(key)
            if last is None or last[0] != marks or now.timestamp() - last[1] >= _HEARTBEAT_S:
                _LAST_LOOK[key] = (marks, now.timestamp())
                due.append(pair)
        if due:
            out[cfg.id] = run_strategy(session, cfg, now, pairs=due)
    for key in [k for k in _LAST_LOOK if k not in alive]:   # stopped, deleted, or a pair removed
        _LAST_LOOK.pop(key, None)
    return out


def check_now(session: Session, rules: Rules, symbol: str, asset_class: str, now: datetime | None = None) -> dict:
    """What the rules read on one pair right now, for the board. Reads only - never trades."""
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings

    now = now or datetime.now(timezone.utc)
    broker = get_broker_for(AssetClass(asset_class), get_or_create_settings(session).broker_map)
    data = load_bars(broker, symbol, rules.tfs(), now, rules.sizes())
    q = _quote(broker, symbol)
    ctx = Ctx(now=now, base_tf=rules.timeframe, risk=_stop_distance(session, rules.stop_atr, data.get(rules.timeframe), q),
              **_quote_ctx(q))
    out: dict = {"symbol": symbol, "paper": bool(getattr(broker, "is_paper", False)),
                 "bars": {tf: len(b) for tf, b in data.items()}, "hours": hours_now(rules.hours, now)}
    try:
        out["market_open"] = bool(broker.market_open(symbol))
    except Exception:  # noqa: BLE001
        out["market_open"] = None
    side_ctx = {s: replace(ctx, risk=_side_risk(session, rules, data, q, s)) if rules.stop else ctx for s in (1, -1)}
    out["long"] = (evaluate(rules.buy_rules(), 1, data, side_ctx[1], "long", off=rules.buy_off())
                   if rules.sides in ("both", "long") else None)
    out["short"] = (evaluate(rules.sell_rules(), -1, data, side_ctx[-1], "short", off=rules.sell_off())
                    if rules.sides in ("both", "short") else None)
    if rules.stop:
        # Where a buy's and a sell's stop would sit now: None on the side the line is against.
        out["stop"] = {name: (None if (w := st_stop(rules.stop, data, s)) is None else
                              {"level": round(w["level"], 6), "line": round(w["line"], 6), "tf": w["tf"]})
                       for name, s in (("long", 1), ("short", -1))}
    # A close rule reads the trade it would close, as in the engine (a SuperTrend already against it at
    # the entry is not a turn): the pair's open trade on each side, if there is one.
    held = _open_positions(session, symbol)
    trade = {d: next((p for p in held if p.direction == d), None) for d in ("long", "short")}
    out["exit_long"] = evaluate(rules.exit_rules, 1, data, replace(ctx, trade=trade["long"]), "exit",
                                off=rules.off_of("exit_rules"))
    out["exit_short"] = evaluate(rules.exit_rules, -1, data, replace(ctx, trade=trade["short"]), "exit",
                                 off=rules.off_of("exit_rules"))
    return out
