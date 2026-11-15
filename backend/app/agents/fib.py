"""The chart's AUTO FIBONACCI (frontend/src/components/autoFib.ts) for the strategy engine, line for line: the same
zig-zag (a turn of 3 x Wilder ATR(14) before a high or a low counts as a swing), the same choice of leg - the trend's
latest FINISHED impulse, so the levels and the targets hold still while price runs to them - the same "broken" rule
(price went past the 1 after the leg's end) and the same position on the leg. Checked identical to the TypeScript on
800 windows of real MT5 bars (2026-11-15: 1m / 5m / 15m / 1h, 8 pairs, every kind of leg).

``place()`` (asked 2026-11-15: "when price has already reached the Fibonacci target zone, or passed it, wait for
the pullback; only enter while there is still distance to the target zone") says where a buy or a sell would open
on that leg, read the trade's way:
  pull    a pullback: price back inside the leg, past its 0.382 (the shallow / golden / deep zones)
  near    at the leg's end: within 0.382 of the swing it ended on
  past    past that swing, on the way to the target zone (before ext 1.272)
  zone    in the target zone, ext 1.272 - 1.618
  beyond  past 1.618: both targets hit
  against no target ahead for this trade: the leg runs the other way (a buy inside a drop), or the leg of the trade's
          own trend failed (price went past its 1)
A leg that BROKE the trade's way (an up leg broken down, for a sell) aims at its break zone, 1.272 - 1.618 beyond
the 1, as the chart's rose zone shows: there "past" = beyond the 1, "zone" = in the break zone, "pull" = back inside
the broken leg."""
from __future__ import annotations

from dataclasses import dataclass

FIB_ATR_MULT = 3.0
ATR_N = 14
MIN_BARS = 30

PLACES = ("pull", "near", "past", "zone", "beyond", "against")
PLACE_NAMES = {"pull": "in a pullback", "near": "at the swing", "past": "past the swing", "zone": "in the target zone",
               "beyond": "past 1.618", "against": "no target ahead"}
# The chart's named zones (autoFib.ts PULLBACK_ZONES), for the reading: (from, to, name) as a retracement.
ZONE_NAMES = ((0.5, 0.618, "golden zone"), (0.618, 0.786, "deep zone"), (0.236, 0.382, "shallow zone"))


@dataclass(frozen=True)
class Swing:
    i: int              # bar index in the candles read
    price: float
    high: bool          # a swing high (else a swing low)


@dataclass(frozen=True)
class Fib:
    trend: str          # "up" | "down"
    a: Swing            # where the leg starts: level 1
    b: Swing            # where it ends: level 0. Both FINISHED swings
    broken: bool        # price has gone past the 1 since the leg's end
    why: str            # trend | break | range | leg (autoFib.ts AutoFib.why)


def atr_series(c, n: int = ATR_N) -> list[float]:
    """Wilder ATR per bar (the bar's own true range until N bars exist) - autoFib.ts atrSeries."""
    out: list[float] = []
    a = 0.0
    for i in range(len(c)):
        if i == 0:
            tr = c[i].high - c[i].low
        else:
            tr = max(c[i].high - c[i].low, abs(c[i].high - c[i - 1].close), abs(c[i].low - c[i - 1].close))
        a = (a * i + tr) / (i + 1) if i < n else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def zigzag(c, mult: float = FIB_ATR_MULT) -> list[Swing]:
    """Alternating swing highs and lows, each turn at least ``mult`` x ATR. The LAST one is the running extreme."""
    if len(c) < 2:
        return []
    atr = atr_series(c)
    out: list[Swing] = []
    d = 0
    hi = Swing(0, c[0].high, True)
    lo = Swing(0, c[0].low, False)
    for i in range(1, len(c)):
        th = mult * atr[i]
        b = c[i]
        if d == 0:
            if b.high > hi.price:
                hi = Swing(i, b.high, True)
            if b.low < lo.price:
                lo = Swing(i, b.low, False)
            if th > 0 and hi.price - lo.price >= th:
                if lo.i < hi.i:
                    out.append(lo)
                    d = 1
                else:
                    out.append(hi)
                    d = -1
        elif d == 1:
            if b.high > hi.price:
                hi = Swing(i, b.high, True)
            elif th > 0 and hi.price - b.low >= th:
                out.append(hi)
                d = -1
                lo = Swing(i, b.low, False)
        else:
            if b.low < lo.price:
                lo = Swing(i, b.low, False)
            elif th > 0 and b.high - lo.price >= th:
                out.append(lo)
                d = 1
                hi = Swing(i, b.high, True)
    if d == 1:
        out.append(hi)
    elif d == -1:
        out.append(lo)
    return out


def _size(a: Swing, b: Swing) -> float:
    return abs(b.price - a.price)


def _containing(c, starts: list[Swing]):
    """The biggest swing price is still INSIDE, from one of ``starts`` (autoFib.ts containingLeg)."""
    n = len(c)
    hi = [0.0] * n
    hi_i = [0] * n
    lo = [0.0] * n
    lo_i = [0] * n
    for i in range(n - 1, -1, -1):
        up = i == n - 1 or c[i].high >= hi[i + 1]
        hi[i] = c[i].high if up else hi[i + 1]
        hi_i[i] = i if up else hi_i[i + 1]
        dn = i == n - 1 or c[i].low <= lo[i + 1]
        lo[i] = c[i].low if dn else lo[i + 1]
        lo_i[i] = i if dn else lo_i[i + 1]
    best = None
    for a in starts:
        k = a.i + 1
        if k >= n:
            continue
        if (hi[k] > a.price) if a.high else (lo[k] < a.price):
            continue
        to = Swing(lo_i[k], lo[k], False) if a.high else Swing(hi_i[k], hi[k], True)
        size = _size(a, to)
        if size > 0 and (best is None or size > best[2]):
            best = (a, to, size)
    return best


def _broken_after(c, a: Swing, b: Swing) -> bool:
    for i in range(b.i + 1, len(c)):
        if (c[i].low < a.price) if b.high else (c[i].high > a.price):
            return True
    return False


def auto_fib(c, mult: float = FIB_ATR_MULT) -> Fib | None:
    """The trend and the FINISHED leg the chart draws its Fibonacci on, or None (autoFib.ts autoFib)."""
    if len(c) < MIN_BARS:
        return None
    p = zigzag(c, mult)[:-1]          # the running extreme is not a swing yet
    n = len(p)
    if n < 2:
        return None
    last = p[-1]

    def leg(a: Swing, b: Swing, why: str) -> Fib:
        return Fib("up" if b.high else "down", a, b, _broken_after(c, a, b), why)

    if n == 2:
        return leg(p[0], p[1], "leg")
    if n >= 4:
        highs = [s for s in p if s.high]
        lows = [s for s in p if not s.high]
        if len(highs) >= 2 and len(lows) >= 2:
            h0, h1 = highs[-2], highs[-1]
            l0, l1 = lows[-2], lows[-1]
            up = h1.price > h0.price and l1.price > l0.price
            down = h1.price < h0.price and l1.price < l0.price
            if up:
                return leg(p[n - 2], last, "trend") if last.high else leg(p[n - 3], p[n - 2], "trend")
            if down:
                return leg(p[n - 2], last, "trend") if not last.high else leg(p[n - 3], p[n - 2], "trend")
            if h1.price < h0.price and l1.price > l0.price:
                box = _containing(c[:last.i + 1], p[max(0, n - 5):n - 1])
                if box:
                    return leg(box[0], box[1], "range")
            return leg(p[n - 2], last, "break") if _size(p[n - 2], last) >= _size(p[n - 3], p[n - 2]) \
                else leg(p[n - 3], p[n - 2], "break")
    return leg(p[n - 2], last, "leg") if _size(p[n - 2], last) >= _size(p[n - 3], p[n - 2]) \
        else leg(p[n - 3], p[n - 2], "leg")


def position(f: Fib, price: float) -> float | None:
    """Where ``price`` sits on the leg: 0 = at its end, 1 = back at its start, under 0 = past the end (targets)."""
    rng = f.b.price - f.a.price
    return (f.b.price - price) / rng if rng else None


def level(f: Fib, r: float, ext: bool = False) -> float:
    """A level's price: a retracement r measured back from the end, an extension (``ext``) beyond it. A break level
    (1.272 / 1.618 with ext False) lies beyond the 1."""
    rng = f.b.price - f.a.price
    return f.a.price + rng * r if ext else f.b.price - rng * r


@dataclass(frozen=True)
class Place:
    place: str                  # one of PLACES
    pos: float                  # the position on the leg (position())
    broken: bool                # the leg is broken (its closed bars, or the price itself past the 1)
    target: float | None        # where the target zone ahead of this trade starts (ext 1.272, or the break zone's)
    target_end: float | None    # ... and ends (1.618)


def place(f: Fib, price: float, side: int) -> Place | None:
    """Where a ``side`` trade (1 buy, -1 sell) at ``price`` would open on leg ``f``, read the trade's way."""
    pos = position(f, price)
    if pos is None:
        return None
    tr = 1 if f.trend == "up" else -1
    broken = f.broken or pos > 1          # price itself past the 1 breaks it at once, as the chart's liveFib does
    if tr == side and not broken:
        where = "pull" if pos > 0.382 else "near" if pos >= 0 else "past" if pos > -0.272 \
            else "zone" if pos >= -0.618 else "beyond"
        return Place(where, pos, False, level(f, 1.272, True), level(f, 1.618, True))
    if tr != side and broken:
        where = "pull" if pos < 1 else "past" if pos < 1.272 else "zone" if pos <= 1.618 else "beyond"
        return Place(where, pos, True, level(f, 1.272), level(f, 1.618))
    return Place("against", pos, broken, None, None)


def zone_name(pos: float) -> str | None:
    """The chart's name for a retracement zone price is in (the golden zone wins at 0.618)."""
    for lo, hi, name in ZONE_NAMES:
        if lo <= pos <= hi:
            return name
    return None
