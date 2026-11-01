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

What a strategy cannot do, whatever its rules say:
  - trade a REAL account. Like the per-pair auto-trader it acts only while the broker is a demo /
    paper account: a live order needs the typed live confirmation, which a robot can't give.
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
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.indicators import (
    _ema_full, adx, atr, bollinger, pivot_levels, pivot_prices, session_vwap, supertrend_series,
)
from app.agents.order_blocks import find_order_blocks
from app.core.logging import get_logger
from app.models.db import AgentRun, Position, StrategyConfig, TradeProposalRecord
from app.models.enums import AssetClass, Direction, PositionStatus, ProposalStatus
from app.models.schemas import Candle

log = get_logger("agents.strategy")

TF_SECONDS = {"5s": 5, "10s": 10, "30s": 30, "1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14_400, "1d": 86_400}
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
_MAX_CONDITIONS = 8
# The levels and zones the "room" filters read: the windows the chart draws its lines (/market/levels,
# 200 bars) and its order-block zones (/market/orderblocks, 500 bars) from.
_LEVEL_BARS = 200
_ZONE_BARS = 500
_TOUCH_ATR = 0.25                          # a swing this close to a level (in ATR) is another touch
_BARS_NEEDED = {"zone_room": _ZONE_BARS}   # conditions that read further back than _BARS


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

    def st_dir(self, p: int, mult: float) -> list[int]:
        return self._m(("st", p, mult), lambda: supertrend_series(self.c, p, mult)["dir"])

    def vwap(self) -> list[float | None]:
        return self._m(("vwap",), lambda: session_vwap(self.c))

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


def _c_supertrend(p, s, b, ctx) -> Result:
    d = b.st_dir(int(p["period"]), float(p["mult"]))
    if not d or d[-1] == 0:
        return None, "not enough bars"
    want = 1 if s > 0 else -1
    if d[-1] != want:
        return False, "red" if d[-1] < 0 else "green"
    fresh = int(p["fresh"])
    if fresh <= 0:
        return True, "green" if want > 0 else "red"
    for ago in range(fresh):
        i = len(d) - 1 - ago
        if i >= 1 and d[i - 1] != want:
            return True, f"flipped {ago} bar{'' if ago == 1 else 's'} ago"
    return False, f"no flip in {fresh} bars"


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
    a = adx(b.c, int(p["period"]))
    if not a:
        return None, "not enough bars"
    leads = (a["plus_di"] > a["minus_di"]) if s > 0 else (a["minus_di"] > a["plus_di"])
    return a["adx"] >= float(p["level"]) and leads, \
        f"ADX {a['adx']:.0f}, {'+DI' if a['plus_di'] > a['minus_di'] else '-DI'} leads"


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


def _c_spread(p, s, b, ctx) -> Result:
    a = atr(b.c, 14)
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
    Spec("supertrend", "Trend", "both",
         "SuperTrend ({period}, {mult}) is green · fresh flip in the last {fresh} bars (0 = not needed)",
         "SuperTrend ({period}, {mult}) is red · fresh flip in the last {fresh} bars (0 = not needed)",
         "SuperTrend ({period}, {mult}) turns against the trade",
         _ST + (Param("fresh", "bars", 0, 0, 50),), _c_supertrend,
         "The ATR trailing line the chart draws. Green = buyers in control. As a close rule it is the "
         "classic exit: the line flipping against you on a faster timeframe."),
    Spec("adx", "Trend", "entry",
         "ADX {period} is at least {level}, buyers (+DI) leading",
         "ADX {period} is at least {level}, sellers (-DI) leading",
         "",
         (Param("period", "period", 14, 5, 50), Param("level", "level", 20, 5, 60)), _c_adx,
         "Trend STRENGTH. Under ~20 the market is chopping: most crossover signals there are noise. "
         "The single best filter to add to an EMA-cross strategy.", suggested=True),
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
         "Stochastic %K ({k}) crosses up through %D from under {zone}",
         "Stochastic %K ({k}) crosses down through %D from over {~zone}",
         "",
         (Param("k", "%K", 5, 3, 50), Param("zone", "zone", 30, 5, 50)), _c_stoch_cross,
         "A pullback ending: the fast oscillator turning up out of the low zone inside an uptrend. "
         "Pair it with a trend condition - alone it fades every strong move.", suggested=True),
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

    @classmethod
    def of(cls, src) -> "Rules":
        return cls(timeframe=src.timeframe, sides=src.sides, entry_long=list(src.entry_long or []),
                   entry_short=list(src.entry_short or []), mirror_short=bool(src.mirror_short),
                   exit_rules=list(src.exit_rules or []), stop_atr=float(src.stop_atr or 1.5))

    def buy_rules(self) -> list:
        return self.entry_long

    def sell_rules(self) -> list:
        return self.entry_long if self.mirror_short else self.entry_short

    def tfs(self) -> set[str]:
        out = {self.timeframe}
        for groups in (self.entry_long, self.entry_short, self.exit_rules):
            for g in groups:
                for c in g:
                    out.add(c.get("tf") or self.timeframe)
        return {t for t in out if t in TF_SECONDS}

    def sizes(self) -> dict[str, int]:
        """Bars to read per timeframe: _BARS, or more where a condition looks further back."""
        out = {t: _BARS for t in self.tfs()}
        for groups in (self.entry_long, self.entry_short, self.exit_rules):
            for g in groups:
                for c in g:
                    tf = c.get("tf") or self.timeframe
                    if tf in out:
                        out[tf] = max(out[tf], _BARS_NEEDED.get(c.get("type", ""), _BARS))
        return out


def evaluate(groups: list, s: int, data: dict[str, Bars], ctx: Ctx, where: str) -> dict:
    """A rule set read now: each group and each of its conditions, and whether any group holds.

    ``where`` is "long" / "short" (entries, judged for that side) or "exit" (judged against a trade
    on side ``s``: each condition for the other side, the exit-only ones for the trade itself)."""
    out = {"ok": False, "groups": []}
    for gi, g in enumerate(groups):
        rows, all_ok = [], bool(g)
        for cond in g:
            spec = CATALOG.get(cond.get("type", ""))
            tf = cond.get("tf") or ctx.base_tf
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
            rows.append({"type": cond.get("type"), "tf": tf, "ok": ok, "detail": detail, "label": label(cond, where)})
            all_ok = all_ok and ok is True
        out["groups"].append({"ok": all_ok, "conditions": rows})
        if all_ok and not out["ok"]:
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
    from app.core.state import get_or_create_risk_config

    a = atr(base.c, 14) if base is not None else None
    if not q or not a or a <= 0:
        return None
    risk_cfg = get_or_create_risk_config(session)
    gate = risk_cfg.max_spread_r_fraction if risk_cfg.spread_gate_enabled else None
    spread, stops = float(q.get("spread") or 0.0), float(q.get("stops_level") or 0.0)
    floor = spread + stops
    if gate and gate > 0:
        floor = max(floor, (spread / gate + spread / 2) * _GATE_SLACK)
    return max(stop_atr * a, floor)


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


def _log(session: Session, cfg: StrategyConfig, symbol: str, event: str, **detail) -> None:
    session.add(AgentRun(agent="strategy", symbol=symbol, event=event,
                         detail={"strategy_id": cfg.id, "name": cfg.name, **detail}))


# --------------------------------------------------------------------------- #
#  The engine
# --------------------------------------------------------------------------- #

def _open(session: Session, cfg: StrategyConfig, symbol: str, ac: str, side: str, ev: dict,
          base: Bars, ctx: Ctx, broker, why: list[str]) -> dict:
    """Size and send the order the rules asked for, through the Risk Manager and the executor."""
    from app.execution.executor import ExecutionBlocked, execute_proposal
    from app.models.schemas import TradeProposal
    from app.risk.service import assess, strategy_cooldowns

    q = _quote(broker, symbol)
    dist = _stop_distance(session, cfg.stop_atr, base, q)
    if q is None or dist is None:
        return {"symbol": symbol, "skipped": "no quote / ATR to set the stop from"}
    s = 1 if side == "long" else -1
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
    dec = assess(session, prop, override_risk_fraction=cfg.risk_pct, **waits)
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
         stop=record.stop_loss, target=record.take_profit, rules=why)
    session.commit()
    log.warning("strategy opened a trade", extra={"strategy": cfg.id, "symbol": symbol, "side": side})
    return {"symbol": symbol, "opened": side, "lots": record.approved_qty}


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


def _run_pair(session: Session, cfg: StrategyConfig, pair: dict, state: dict, now: datetime) -> dict:
    from app.brokers.registry import get_broker_for
    from app.core.state import get_or_create_settings, kill_switch_active

    symbol, ac = pair.get("symbol") or "", pair.get("asset_class") or "forex"
    broker = get_broker_for(AssetClass(ac), get_or_create_settings(session).broker_map)
    if not getattr(broker, "is_paper", False):
        return _note(state, symbol, "live account - strategies only trade a demo / paper account", now)
    rules = Rules.of(cfg)
    data = load_bars(broker, symbol, rules.tfs(), now, rules.sizes())
    base = data.get(cfg.timeframe)
    if base is None or len(base) < _MIN_BARS:
        return _note(state, symbol, f"reading the {cfg.timeframe} candles", now)
    q = _quote(broker, symbol)
    ctx = Ctx(now=now, base_tf=cfg.timeframe, risk=_stop_distance(session, cfg.stop_atr, base, q), **_quote_ctx(q))
    own = strategy_source(cfg)

    # 1) The trades it manages: its own, and the user's on this pair when asked.
    mine = [p for p in _open_positions(session, symbol)
            if p.source == own or (cfg.manage_manual and (p.source or "manual") == "manual")]
    for pos in mine:
        if not rules.exit_rules:
            break
        s = 1 if pos.direction == "long" else -1
        ev = evaluate(rules.exit_rules, s, data, replace(ctx, trade=pos), "exit")
        if ev["ok"]:
            why = [c["label"] for c in ev["groups"][ev["group"]]["conditions"]]
            done = _close(session, cfg, pos, broker, why)
            if done:
                state.setdefault(symbol, {})["acted_bar"] = base.c[-1].ts.isoformat()
                return done
    if any(p.source == own for p in mine):
        return _note(state, symbol, "in a trade - watching the close rules", now)
    if _open_positions(session, symbol):
        return _note(state, symbol, "you have a trade open on this pair - no new entry", now)

    # 2) A new entry: at most one per closed bar of the base timeframe.
    st = state.setdefault(symbol, {})
    bar = base.c[-1].ts.isoformat()
    if st.get("acted_bar") == bar:
        return {"symbol": symbol, "note": st.get("note")}
    if kill_switch_active(session):
        return _note(state, symbol, "kill-switch on - no new trades", now)
    if _stale(base, now) or not broker.market_open(symbol):
        return _note(state, symbol, "market closed / no fresh candles", now)
    if _trades_today(session, cfg, now) >= cfg.max_trades_day:
        return _note(state, symbol, f"{cfg.max_trades_day} trades today - the daily cap", now)
    for side, s, groups in (("long", 1, rules.buy_rules()), ("short", -1, rules.sell_rules())):
        if cfg.sides not in ("both", side) or not groups:
            continue
        ev = evaluate(groups, s, data, ctx, side)
        if ev["ok"]:
            st["acted_bar"] = bar        # one try per bar, filled or refused
            why = [c["label"] for c in ev["groups"][ev["group"]]["conditions"]]
            res = _open(session, cfg, symbol, pair.get("asset_class") or "forex", side, ev, base, ctx, broker, why)
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
    results = []
    for pair in (cfg.pairs or []) if pairs is None else pairs:
        try:
            results.append(_run_pair(session, cfg, pair, state, now))
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
                 "bars": {tf: len(b) for tf, b in data.items()}}
    try:
        out["market_open"] = bool(broker.market_open(symbol))
    except Exception:  # noqa: BLE001
        out["market_open"] = None
    out["long"] = evaluate(rules.buy_rules(), 1, data, ctx, "long") if rules.sides in ("both", "long") else None
    out["short"] = evaluate(rules.sell_rules(), -1, data, ctx, "short") if rules.sides in ("both", "short") else None
    out["exit_long"] = evaluate(rules.exit_rules, 1, data, ctx, "exit")
    out["exit_short"] = evaluate(rules.exit_rules, -1, data, ctx, "exit")
    return out
