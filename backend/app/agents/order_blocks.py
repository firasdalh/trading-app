"""Order blocks - the candle a move departed from, and the zone price tends to revisit.

WHAT AN ORDER BLOCK IS
----------------------
A large participant cannot fill size at one price. Before an impulsive move they work an order
inside a tight area; the last candle AGAINST the coming move marks where that happened. When price
later returns to that area, the unfilled remainder of those orders is what it is returning to.

So an order block is three things at once, and all three are required here:

  1. an ORIGIN candle - the last down candle before an up move (bullish), or the last up candle
     before a down move (bearish);
  2. DISPLACEMENT - the move away has to be impulsive, measured in ATR, not merely upward;
  3. a BREAK OF STRUCTURE - that move has to take out a confirmed swing. Without a break it is a
     pullback inside the existing swing, and the "block" is just a red candle.

Drop any of the three and the detector fires on every other bar, which is the usual reason order
blocks look useless on a chart.

HONESTY ABOUT TIMING (read this before using it in a backtest)
--------------------------------------------------------------
An order block cannot be known at its origin candle. It becomes visible only once the displacement
and the structure break have happened, several bars later. That is a property of the idea, not a
flaw in the code - but it is exactly the shape of mistake that makes a backtest lie.

Every block therefore carries ``confirmed_index``: the bar at which it became knowable. A caller
replaying history MUST filter on it (``ob.confirmed_index <= i``). ``origin_index`` is where the
zone is drawn; ``confirmed_index`` is when you were allowed to know. See `visible_at()`.

WHAT CANNOT BE MEASURED ON THIS DATA
------------------------------------
MT5 CFDs give tick_volume - a count of price changes - not traded volume, and no bid/ask split.
Every "institutional absorption", "delta" or "effort vs result" refinement of this idea is
unmeasurable on this feed, so none of them is scored here. Volume appears only as a weak
confirmation that something happened, never as proof of who did it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.agents.indicators import _swing_pivots, atr
from app.core.logging import get_logger
from app.models.schemas import Candle

log = get_logger("agents.order_blocks")

# Swing-pivot confirmation window. Same 3/3 the rest of the engine uses (market_structure), so an
# "order block" here breaks the same swings the trend read is built from.
_PIVOT_L = 3
_PIVOT_R = 3

# The impulse away from the block must cover at least this many ATR. Below it the move is drift and
# the candle before it is not an order block, just the last red bar of a quiet stretch.
_MIN_DISPLACEMENT_ATR = 1.2
# How far back from the breaking bar to hunt for the origin candle.
_ORIGIN_LOOKBACK = 12
# An impulse is fast. Allowing a break to arrive any number of bars after the origin let a slow
# DRIFT qualify: as the drift bars shrink, ATR collapses with them, so a 1.1-point crawl over 19
# bars measured as "15 ATR of displacement". A move that takes this long is not an impulse.
_MAX_IMPULSE_BARS = 10
# Two zones closer than this (in ATR) are the same area seen twice - keep the stronger.
_MERGE_ATR = 0.35
# Blocks older than this stop being interesting; the tape has moved on.
_MAX_AGE_BARS = 400
# An origin candle wider than this many ATR is not a precise area - it is a bar with a long wick.
# Past it, the zone is taken from the candle's BODY (open..close), which is where the business was
# done; the wick is where it was rejected. Without this a single 7-ATR gold bar produced a zone so
# wide that price was permanently "inside" it.
_MAX_ZONE_ATR = 1.5


@dataclass
class OrderBlock:
    """One zone, with everything needed to draw it, judge it and replay it honestly."""

    kind: str                      # "bullish" (demand) | "bearish" (supply)
    top: float
    bottom: float
    origin_index: int              # bar the zone is drawn at
    origin_ts: datetime
    confirmed_index: int           # bar it became KNOWABLE - see the module docstring
    confirmed_ts: datetime
    broke_level: float             # the swing the impulse took out
    displacement_atr: float
    fvg: bool = False              # an imbalance inside the impulse (a stronger block)
    touches: int = 0               # times price has returned into the zone since
    mitigated: bool = False        # price has traded back into it at least once
    invalidated: bool = False      # price closed clean through the far side - the block failed
    strength: int = 0              # 0..100, see `_score`
    age_bars: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "top": round(self.top, 8), "bottom": round(self.bottom, 8),
            "mid": round(self.mid, 8),
            "origin_index": self.origin_index, "origin_ts": self.origin_ts.isoformat(),
            "confirmed_index": self.confirmed_index, "confirmed_ts": self.confirmed_ts.isoformat(),
            "broke_level": round(self.broke_level, 8),
            "displacement_atr": round(self.displacement_atr, 2),
            "fvg": self.fvg, "touches": self.touches, "mitigated": self.mitigated,
            "invalidated": self.invalidated, "strength": self.strength,
            "age_bars": self.age_bars, "reasons": self.reasons,
        }


def visible_at(blocks: list[OrderBlock], index: int) -> list[OrderBlock]:
    """The blocks a trader could actually have known at bar ``index``.

    The one function that stands between this module and a look-ahead backtest. An order block is
    confirmed several bars after its origin candle, so replaying with the full list would hand the
    engine zones that had not been drawn yet - the same class of error that invalidated every
    backtest in this repo before the 2026-09 audit.
    """
    return [b for b in blocks if b.confirmed_index <= index]


def _atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    """Wilder ATR as of EVERY bar, computed in one pass.

    Displacement has to be judged against the volatility that prevailed BEFORE the move - that is
    what makes a move impulsive rather than merely large, and measuring against end-of-series ATR
    would quietly use information from after the block.

    Doing that per candidate by slicing (`atr(candles[:i+1])`) was O(n) work plus a fresh list
    allocation for each of ~40 candidates, which is most of why one call cost 7.8 ms - about 28
    minutes of pure overhead across a 33-symbol backtest. One pass, then O(1) lookups.
    """
    n = len(candles)
    out = [0.0] * n
    if n < 2:
        return out
    trs: list[float] = [candles[0].high - candles[0].low]
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    # Running sum for the seed window. Writing it as sum(trs[:i+1]) inside the loop looks harmless
    # and is quadratic - it made this function three times SLOWER than the per-candidate slicing it
    # was meant to replace (7.8 ms -> 21.7 ms).
    run_sum = 0.0
    run = 0.0
    for i in range(n):
        if i < period:
            run_sum += trs[i]
            run = run_sum / (i + 1)
        else:
            run = (run * (period - 1) + trs[i]) / period      # Wilder smoothing
        out[i] = run
    return out


def _zone(c: Candle, atr_v: float) -> tuple[float, float]:
    """(top, bottom) for an origin candle - full range normally, body when the candle is huge."""
    if atr_v > 0 and (c.high - c.low) > _MAX_ZONE_ATR * atr_v:
        return max(c.open, c.close), min(c.open, c.close)
    return c.high, c.low


def _has_fvg(candles: list[Candle], start: int, end: int, bullish: bool) -> bool:
    """A fair-value gap inside the impulse: three bars where the middle one leaves a hole.

    Bullish: bar i-1's HIGH is below bar i+1's LOW - price moved so fast it skipped a range nobody
    traded. An order block with a gap in its departure is the one worth drawing; it is the clearest
    evidence the move was urgent rather than drifting.
    """
    for i in range(max(start + 1, 1), min(end, len(candles) - 1)):
        if bullish and candles[i - 1].high < candles[i + 1].low:
            return True
        if not bullish and candles[i - 1].low > candles[i + 1].high:
            return True
    return False


def _vol_at(candles: list[Candle], i: int, period: int = 20) -> float | None:
    """Tick-volume of bar ``i`` relative to the ``period`` bars before it.

    Per block, at the bar that BROKE structure. The first version used `volume_ratio(candles)`,
    which is the CURRENT bar's activity, and stamped it on every block as "activity 1.6x average on
    the break" - a specific claim about each block that was really a claim about right now.
    """
    if i < period:
        return None
    prior = [c.volume for c in candles[i - period:i]]
    avg = sum(prior) / period
    return round(candles[i].volume / avg, 2) if avg else None


def _score(ob: OrderBlock, atr_v: float, vol_ratio: float | None) -> None:
    """0-100, and the plain-language reasons behind it.

    Weighted the way these actually earn money: displacement and a clean break first (they say the
    move was real), the imbalance next, and freshness last. Volume is worth a token 5 points because
    tick volume cannot tell you WHO traded - see the module docstring.
    """
    pts, why = 0, []
    d = ob.displacement_atr
    if d >= 3.0:
        pts += 40; why.append(f"violent departure ({d:.1f} ATR)")
    elif d >= 2.0:
        pts += 30; why.append(f"strong departure ({d:.1f} ATR)")
    elif d >= _MIN_DISPLACEMENT_ATR:
        pts += 18; why.append(f"clean departure ({d:.1f} ATR)")

    pts += 20
    why.append("broke a confirmed swing")          # required to exist at all

    if ob.fvg:
        pts += 20; why.append("left an imbalance (fair-value gap) on the way out")

    if ob.touches == 0:
        pts += 15; why.append("never retested - the first return is the clean one")
    elif ob.touches == 1:
        pts += 7; why.append("tested once already")
    elif ob.touches <= 3:
        why.append(f"tested {ob.touches} times - weaker each time")
    else:
        # Each visit spends more of whatever was resting there. A zone price keeps wandering back
        # into is not holding anyone's orders; it is just an area price likes.
        pts -= 15
        why.append(f"tested {ob.touches} times - largely spent")

    if ob.age_bars <= 50:
        pts += 5; why.append("recent")
    elif ob.age_bars > 200:
        pts -= 5; why.append("old")

    if vol_ratio is not None and vol_ratio >= 1.5:
        pts += 5; why.append(f"activity {vol_ratio:.1f}x average on the break")

    if ob.invalidated:
        pts = min(pts, 15); why.append("price closed through it - treat as broken")

    ob.strength = max(0, min(100, pts))
    ob.reasons = why


def _mitigation(candles: list[Candle], ob: OrderBlock, from_index: int) -> None:
    """Count returns into the zone, and decide whether it still stands.

    MITIGATED = price has traded back into the zone at least once (the orders resting there have
    started to be filled). A mitigated block is not dead - the first retest is the classic entry -
    but each further touch spends more of what was left.

    INVALIDATED = a bar CLOSED clean through the far side. A wick through is a test; a close through
    is the level failing, and a failed block drawn on a chart is worse than no block.
    """
    # A "touch" is a RETURN to the zone, not a bar that happens to overlap it. Counting every
    # overlapping bar made a zone price drifted through for twenty bars read as twenty tests; one
    # gold zone came back with 77. Count the transition from outside to inside instead.
    inside = False
    for i in range(from_index + 1, len(candles)):
        c = candles[i]
        overlap = c.low <= ob.top and c.high >= ob.bottom
        if overlap and not inside:
            ob.touches += 1
            ob.mitigated = True
        inside = overlap
        # The last bar is normally still FORMING: its "close" is just the latest tick. Invalidating
        # on it made a zone vanish whenever price poked through intrabar and reappear when it came
        # back (UK100 1h demand, 2026-10-21). Only a finished bar's close breaks a zone.
        if i == len(candles) - 1:
            break
        if ob.kind == "bullish" and c.close < ob.bottom:
            ob.invalidated = True
            break
        if ob.kind == "bearish" and c.close > ob.top:
            ob.invalidated = True
            break


def _merge(blocks: list[OrderBlock], atr_v: float) -> list[OrderBlock]:
    """Two zones within _MERGE_ATR of each other are one area seen twice - keep the stronger."""
    out: list[OrderBlock] = []
    for b in sorted(blocks, key=lambda x: -x.strength):
        if any(o.kind == b.kind and abs(o.mid - b.mid) < _MERGE_ATR * atr_v for o in out):
            continue
        out.append(b)
    return out


def find_order_blocks(candles: list[Candle], *, max_blocks: int = 8,
                      include_invalidated: bool = False) -> list[OrderBlock]:
    """Every order block still worth drawing, strongest first.

    Pure function of the candles handed in - no clock, no network, no database - so it is testable
    and behaves identically in the live desk and the backtest harness.
    """
    n = len(candles)
    atr_v = atr(candles) or 0.0
    if n < _PIVOT_L + _PIVOT_R + 20 or atr_v <= 0:
        return []

    atrs = _atr_series(candles)
    # The LAST candle handed in is treated as still forming (live, it is MT5's current bar: its close
    # is only the latest tick). It may touch a zone, but it never confirms a swing, creates a zone or
    # breaks one - otherwise a zone appeared when price ticked through a level mid-bar and vanished
    # when it ticked back (UK100 1h "Supply 58", 2026-10-21). Pivots are found without it, and the
    # break that creates a zone must be a finished bar.
    highs, lows = _swing_pivots(candles[:-1], _PIVOT_L, _PIVOT_R, 0)
    found: list[OrderBlock] = []

    # ---- bullish: an up-move that closes above a confirmed swing HIGH ----------------------
    for p in highs:
        level = candles[p].high
        b = next((i for i in range(p + _PIVOT_R + 1, n - 1) if candles[i].close > level), None)
        if b is None:
            continue
        # the last DOWN candle before the impulse - where the buying was worked
        o = next((i for i in range(b, max(b - _ORIGIN_LOOKBACK, 0) - 1, -1)
                  if candles[i].close < candles[i].open), None)
        if o is None:
            continue
        if b - o > _MAX_IMPULSE_BARS:
            continue
        a0 = atrs[o]
        if a0 <= 0:
            continue
        disp = (candles[b].close - candles[o].low) / a0
        if disp < _MIN_DISPLACEMENT_ATR:
            continue
        found.append(OrderBlock(
            kind="bullish", top=_zone(candles[o], a0)[0], bottom=_zone(candles[o], a0)[1],
            origin_index=o, origin_ts=candles[o].ts,
            confirmed_index=b, confirmed_ts=candles[b].ts,
            broke_level=level, displacement_atr=disp,
            fvg=_has_fvg(candles, o, b, bullish=True), age_bars=n - 1 - o,
        ))

    # ---- bearish: a down-move that closes below a confirmed swing LOW ----------------------
    for p in lows:
        level = candles[p].low
        b = next((i for i in range(p + _PIVOT_R + 1, n - 1) if candles[i].close < level), None)
        if b is None:
            continue
        o = next((i for i in range(b, max(b - _ORIGIN_LOOKBACK, 0) - 1, -1)
                  if candles[i].close > candles[i].open), None)
        if o is None:
            continue
        if b - o > _MAX_IMPULSE_BARS:
            continue
        a0 = atrs[o]
        if a0 <= 0:
            continue
        disp = (candles[o].high - candles[b].close) / a0
        if disp < _MIN_DISPLACEMENT_ATR:
            continue
        found.append(OrderBlock(
            kind="bearish", top=_zone(candles[o], a0)[0], bottom=_zone(candles[o], a0)[1],
            origin_index=o, origin_ts=candles[o].ts,
            confirmed_index=b, confirmed_ts=candles[b].ts,
            broke_level=level, displacement_atr=disp,
            fvg=_has_fvg(candles, o, b, bullish=False), age_bars=n - 1 - o,
        ))

    for ob in found:
        _mitigation(candles, ob, ob.confirmed_index)
        _score(ob, atr_v, _vol_at(candles, ob.confirmed_index))

    kept = [b for b in found if b.age_bars <= _MAX_AGE_BARS
            and (include_invalidated or not b.invalidated)]
    return _merge(kept, atr_v)[:max_blocks]


def opposing_block(blocks: list[OrderBlock], direction: str, entry: float,
                   *, min_strength: int = 60, within_atr: float | None = None,
                   atr_v: float = 0.0) -> OrderBlock | None:
    """The nearest block standing in a trade's PATH, if one is close enough to matter.

    For a long that means the nearest unmitigated SUPPLY zone above entry; for a short, demand
    below. Mitigated zones are skipped: price has already traded into them, and whatever was
    resting there has started to be spent, so they are a far weaker argument than a virgin one.

    ``within_atr`` bounds how far ahead counts as "in the way". Left as None, the nearest qualifying
    block is returned however far away it is — which is what a TARGET wants, while a WALL check
    wants it bounded.
    """
    want = "bearish" if direction == "long" else "bullish"
    ahead = [
        b for b in blocks
        if b.kind == want and not b.mitigated and not b.invalidated and b.strength >= min_strength
        and (b.bottom > entry if direction == "long" else b.top < entry)
    ]
    if within_atr is not None and atr_v > 0:
        reach = within_atr * atr_v
        ahead = [b for b in ahead
                 if (b.bottom - entry if direction == "long" else entry - b.top) <= reach]
    if not ahead:
        return None
    return min(ahead, key=lambda b: abs((b.bottom if direction == "long" else b.top) - entry))
