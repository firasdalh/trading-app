"""Trader positioning: crowding, not direction.

WHY THERE IS NO LIVE SOURCE HERE
--------------------------------
The spec asks for Forex Factory's per-pair positioning (Long/Short Traders %, Long/Short Lots %).
That data lives only on forexfactory.com/market/<pair>, which is served behind a Cloudflare managed
challenge - even /robots.txt returns the interstitial. Reading it from a program means defeating a
technical access-control measure, which the spec itself rules out, so this module ships WITHOUT a
Forex Factory scraper.

What it does ship is everything around that source: the snapshot store, the crowding model, the
staleness rules and the UI contract. Point `set_positioning_source()` at any feed the user is
entitled to use - a broker sentiment endpoint, an FXCM/OANDA/IG positioning API, a licensed FF
feed - and the whole layer lights up unchanged.

THE ONE RULE
------------
Crowding is a MAGNITUDE, never a direction. "84% of lots are long" is a statement about fragility,
not a sell signal; crowded markets stay crowded for months. `crowding_score` is therefore computed
from |imbalance| and is deliberately unsigned, and `bias_label` is descriptive text that no code
branches on.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from app.core.logging import get_logger

log = get_logger("macro.positioning")

# Below this many stored snapshots a percentile is meaningless, so the layer reports "building
# history" instead of a number that looks measured but is not.
MIN_HISTORY = 20
DEFAULT_STALE_AFTER_SEC = 6 * 3600


@dataclass
class PositioningSnapshot:
    """One reading of the crowd, as percentages that each pair sums to 100."""

    symbol: str
    long_traders_pct: float | None = None
    short_traders_pct: float | None = None
    long_lots_pct: float | None = None
    short_lots_pct: float | None = None
    trader_count: int | None = None
    lot_count: float | None = None
    source: str = "unavailable"
    source_timestamp: datetime | None = None
    fetched_at: datetime | None = None

    @property
    def trader_imbalance(self) -> float | None:
        if self.long_traders_pct is None or self.short_traders_pct is None:
            return None
        return self.long_traders_pct - self.short_traders_pct

    @property
    def lot_imbalance(self) -> float | None:
        if self.long_lots_pct is None or self.short_lots_pct is None:
            return None
        return self.long_lots_pct - self.short_lots_pct

    def age_seconds(self, now: datetime | None = None) -> float | None:
        ref = self.source_timestamp or self.fetched_at
        if ref is None:
            return None
        now = now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return (now - ref).total_seconds()


class PositioningSource(Protocol):
    """Anything that can report the crowd for a symbol. See the module docstring for why the
    Forex Factory implementation is absent."""

    name: str

    def get_positioning(self, symbol: str) -> PositioningSnapshot | None: ...


class UnavailablePositioningSource:
    """The default. Reports nothing, loudly, rather than inventing a number."""

    name = "unavailable"

    def get_positioning(self, symbol: str) -> PositioningSnapshot | None:
        return None


@dataclass
class PositioningContext:
    """What the AI brief and the UI receive. Every field is descriptive; none is a signal."""

    available: bool = False
    stale: bool = False
    symbol: str = ""
    long_traders_pct: float | None = None
    short_traders_pct: float | None = None
    long_lots_pct: float | None = None
    short_lots_pct: float | None = None
    trader_imbalance: float | None = None
    lot_imbalance: float | None = None
    crowding_score: int = 0              # 0..100 DEGREE of crowding, unsigned by construction
    crowding_band: str = "UNKNOWN"       # LOW | MODERATE | ELEVATED | HIGH | EXTREME
    bias_label: str = ""                 # e.g. "strongly long-crowded" - text only
    percentile: float | None = None      # where this sits in the pair's own history
    trend: str = "unknown"               # increasing | stable | decreasing | unknown
    samples: int = 0
    snapshot_age_seconds: float | None = None
    source: str = "unavailable"
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available, "stale": self.stale, "symbol": self.symbol,
            "long_traders_pct": self.long_traders_pct, "short_traders_pct": self.short_traders_pct,
            "long_lots_pct": self.long_lots_pct, "short_lots_pct": self.short_lots_pct,
            "trader_imbalance": self.trader_imbalance, "lot_imbalance": self.lot_imbalance,
            "crowding_score": self.crowding_score, "crowding_band": self.crowding_band,
            "bias_label": self.bias_label, "percentile": self.percentile, "trend": self.trend,
            "samples": self.samples, "snapshot_age_seconds": self.snapshot_age_seconds,
            "source": self.source, "note": self.note,
            # Repeated in the payload so it reaches the model and the UI together, and neither can
            # present this as a trade signal by accident.
            "disclaimer": "Positioning is context only - not a directional signal.",
        }


def _band(score: int) -> str:
    if score <= 20:
        return "LOW"
    if score <= 40:
        return "MODERATE"
    if score <= 60:
        return "ELEVATED"
    if score <= 80:
        return "HIGH"
    return "EXTREME"


def _bias_label(lot_imb: float | None, trader_imb: float | None) -> str:
    """Plain words for WHICH WAY the crowd leans. Descriptive only - nothing branches on this."""
    v = lot_imb if lot_imb is not None else trader_imb
    if v is None:
        return ""
    side = "long" if v > 0 else "short"
    a = abs(v)
    degree = "mildly" if a < 20 else "moderately" if a < 40 else "strongly" if a < 65 else "extremely"
    return f"{degree} {side}-crowded"


# Lots are weighted above head-count on purpose: one funded account carrying 200 lots is more of
# the market's real exposure than 200 retail accounts carrying 0.01 each, and it is exposure that
# has to be unwound. Neither weight is measured - both are here to be ablated (spec section 17).
_LOT_WEIGHT = 0.65
_TRADER_WEIGHT = 0.35


def build_context(
    latest: PositioningSnapshot | None,
    history: list[PositioningSnapshot] | None = None,
    *,
    now: datetime | None = None,
    stale_after_sec: int = DEFAULT_STALE_AFTER_SEC,
) -> PositioningContext:
    """Crowding context for one symbol from its latest snapshot plus its own history.

    ``history`` is oldest-to-newest and should EXCLUDE ``latest``; it is what turns a raw
    percentage into "extreme for this pair", which is the only way the word extreme means anything.
    A pair that sits at 70% long all year is not crowded at 70%.
    """
    now = now or datetime.now(timezone.utc)
    if latest is None:
        return PositioningContext(
            available=False, crowding_band="UNKNOWN",
            note="POSITIONING DATA UNAVAILABLE - no permitted source is configured",
        )

    ctx = PositioningContext(
        available=True, symbol=latest.symbol,
        long_traders_pct=latest.long_traders_pct, short_traders_pct=latest.short_traders_pct,
        long_lots_pct=latest.long_lots_pct, short_lots_pct=latest.short_lots_pct,
        trader_imbalance=latest.trader_imbalance, lot_imbalance=latest.lot_imbalance,
        source=latest.source, snapshot_age_seconds=latest.age_seconds(now),
    )
    if ctx.snapshot_age_seconds is not None and ctx.snapshot_age_seconds > stale_after_sec:
        ctx.stale = True
        ctx.note = "POSITIONING DATA STALE"

    # Raw crowding: a weighted blend of the two |imbalances|, each already on a 0-100 scale.
    parts, weights = [], []
    if latest.lot_imbalance is not None:
        parts.append(abs(latest.lot_imbalance)); weights.append(_LOT_WEIGHT)
    if latest.trader_imbalance is not None:
        parts.append(abs(latest.trader_imbalance)); weights.append(_TRADER_WEIGHT)
    raw = sum(p * w for p, w in zip(parts, weights)) / sum(weights) if parts else 0.0

    hist = [h for h in (history or []) if h.lot_imbalance is not None or h.trader_imbalance is not None]
    ctx.samples = len(hist)
    if len(hist) >= MIN_HISTORY:
        past = [abs(h.lot_imbalance if h.lot_imbalance is not None else h.trader_imbalance)
                for h in hist]
        ctx.percentile = round(100.0 * sum(1 for p in past if p <= raw) / len(past), 1)
        # Blend the absolute reading with where it sits historically, so "extreme" is relative to
        # the pair rather than to a hard-coded percentage the spec explicitly warned against.
        score = 0.5 * raw + 0.5 * ctx.percentile
        recent, older = past[-5:], past[:-5]
        if older:
            d = statistics.fmean(recent) - statistics.fmean(older)
            ctx.trend = "increasing" if d > 5 else "decreasing" if d < -5 else "stable"
    else:
        score = raw
        ctx.note = (ctx.note + " | " if ctx.note else "") + \
            f"building history ({len(hist)}/{MIN_HISTORY} snapshots) - percentile not yet meaningful"

    ctx.crowding_score = int(max(0, min(100, round(score))))
    ctx.crowding_band = _band(ctx.crowding_score)
    ctx.bias_label = _bias_label(latest.lot_imbalance, latest.trader_imbalance)
    return ctx
