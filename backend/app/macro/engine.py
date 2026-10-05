"""News-risk engine: how dangerous is the clock right now for this instrument?

This answers one question - "is this a bad MOMENT to open this trade?" - and deliberately cannot
answer "which way will it go?". A scheduled release is a volatility event with an unknown sign; the
only honest responses are proceed, warn, or wait.

Everything here is a pure function of its inputs (events + config + now), so it is fully testable
without a network or a database, matching the convention in app/risk/manager.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.data.providers import _COUNTRY_TO_CCY, CalendarEvent

# Score bands. Deliberately configurable: these are a starting point, NOT a measured optimum, and
# section 17 of the spec exists precisely so they can be ablated against the baseline.
DEFAULT_BANDS: tuple[tuple[int, str], ...] = (
    (20, "LOW"), (40, "MODERATE"), (60, "ELEVATED"), (80, "HIGH"), (100, "SEVERE"),
)


@dataclass
class MacroWindows:
    """Timing windows around an event, in minutes. All configurable; none are proven."""

    warning_minutes: int = 120        # start paying attention
    lock_before_minutes: int = 30     # stop opening new trades
    lock_after_minutes: int = 30      # stay out while the first spike unwinds
    normalize_minutes: int = 60       # after this, treat the tape as normal again

    # Score at which the gate escalates. Separate from the bands so you can move one without
    # the other while ablating.
    warn_score: int = 40
    wait_score: int = 70


@dataclass
class EventRef:
    """One calendar event, flattened for storage, the AI brief and the UI."""

    title: str
    currency: str
    impact: str
    when: datetime
    minutes_away: float
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None

    def as_dict(self) -> dict:
        return {
            "title": self.title, "currency": self.currency, "impact": self.impact,
            "when": self.when.isoformat(), "minutes_away": round(self.minutes_away, 1),
            "forecast": self.forecast, "previous": self.previous, "actual": self.actual,
        }


@dataclass
class NewsRisk:
    """The verdict. ``action`` is the only thing the engine acts on, and it is never a direction."""

    score: int = 0
    band: str = "LOW"
    action: str = "proceed"           # proceed | warn | wait
    reason: str = ""
    next_high: EventRef | None = None
    next_medium: EventRef | None = None
    recent: list[EventRef] = field(default_factory=list)
    base_currency: str | None = None
    quote_currency: str | None = None
    affects_base: bool = False
    affects_quote: bool = False
    both_sides: bool = False
    available: bool = True            # False => feed down; see MacroConfig.fail_mode
    stale: bool = False
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "score": self.score, "band": self.band, "action": self.action, "reason": self.reason,
            "next_high": self.next_high.as_dict() if self.next_high else None,
            "next_medium": self.next_medium.as_dict() if self.next_medium else None,
            "recent": [e.as_dict() for e in self.recent],
            "base_currency": self.base_currency, "quote_currency": self.quote_currency,
            "affects_base": self.affects_base, "affects_quote": self.affects_quote,
            "both_sides": self.both_sides, "available": self.available, "stale": self.stale,
            "note": self.note,
        }


def band_for(score: int, bands: tuple[tuple[int, str], ...] = DEFAULT_BANDS) -> str:
    for ceiling, label in bands:
        if score <= ceiling:
            return label
    return bands[-1][1]


def _proximity(minutes_away: float, warning_minutes: int) -> float:
    """1.0 at the moment of release, falling linearly to 0.0 at the edge of the warning window.

    Distance is what makes an event dangerous, so the score has to be continuous in it: a step
    function would make a trade fine at 31 minutes and forbidden at 29, which is not how risk
    behaves and is impossible to ablate sensibly.
    """
    if warning_minutes <= 0:
        return 0.0
    d = abs(minutes_away)
    return max(0.0, min(1.0, 1.0 - d / warning_minutes))


# Weights per impact class. A high-impact release at t=0 alone reaches 60; the lock-window floor
# below is what actually escalates it to a WAIT, so these only shape the ramp.
_WEIGHT = {"high": 60.0, "medium": 25.0, "low": 0.0, "holiday": 0.0}
_BOTH_SIDES_BONUS = 10.0


def _ccy(event_country: str | None) -> str:
    """CalendarEvent.country holds a COUNTRY code ("US", "EU"); everything the macro layer talks
    about is a CURRENCY ("USD", "EUR"). Converting here is what lets `affects_base` work at all -
    comparing "US" against "USD" silently matched nothing, so `both_sides` could never fire."""
    c = (event_country or "").upper()
    return _COUNTRY_TO_CCY.get(c, c)


def _to_ref(e: CalendarEvent, now: datetime) -> EventRef:
    return EventRef(
        title=e.label,
        currency=_ccy(e.country),
        impact=e.importance,
        when=e.when,
        minutes_away=(e.when - now).total_seconds() / 60.0,
        forecast=e.forecast, previous=e.previous, actual=e.actual,
    )


def assess(
    events: list[CalendarEvent],
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    now: datetime | None = None,
    windows: MacroWindows | None = None,
    available: bool = True,
    stale: bool = False,
) -> NewsRisk:
    """Score the timing risk around ``now`` for an instrument whose legs are base/quote.

    ``events`` should already be filtered to the instrument's currencies. Events are matched by
    CURRENCY, so a EURUSD trade is assessed against both EUR and USD releases; a single-currency
    instrument (an index, gold) simply has no quote leg.
    """
    w = windows or MacroWindows()
    now = now or datetime.now(timezone.utc)
    risk = NewsRisk(base_currency=base_currency, quote_currency=quote_currency,
                    available=available, stale=stale)

    if not available:
        risk.note = "NEWS DATA UNAVAILABLE - calendar feed could not be read"
        risk.reason = risk.note
        return risk

    upcoming = sorted([e for e in events if e.when >= now], key=lambda e: e.when)
    just_out = sorted(
        [e for e in events
         if 0 < (now - e.when).total_seconds() / 60.0 <= w.lock_after_minutes
         and e.importance in ("high", "medium")],
        key=lambda e: e.when, reverse=True,
    )
    risk.recent = [_to_ref(e, now) for e in just_out[:5]]

    nh = next((e for e in upcoming if e.importance == "high"), None)
    nm = next((e for e in upcoming if e.importance == "medium"), None)
    risk.next_high = _to_ref(nh, now) if nh else None
    risk.next_medium = _to_ref(nm, now) if nm else None
    return _score(risk, upcoming, just_out, now, w, base_currency, quote_currency)


def _score(risk: NewsRisk, upcoming, just_out, now, w: MacroWindows,
           base: str | None, quote: str | None) -> NewsRisk:
    """Turn the matched events into a 0-100 score and one of proceed / warn / wait."""
    score = 0.0
    drivers: list[str] = []

    # 1. The next scheduled release, weighted by impact and how close it is.
    for ev in (risk.next_high, risk.next_medium):
        if ev is None or ev.minutes_away > w.warning_minutes:
            continue
        contrib = _WEIGHT.get(ev.impact, 0.0) * _proximity(ev.minutes_away, w.warning_minutes)
        if contrib > 0:
            score += contrib
            drivers.append(f"{ev.currency} {ev.title} in {ev.minutes_away:.0f}m ({ev.impact})")

    # 2. A release that has JUST landed. The spike and its retrace are the most hostile tape there
    #    is, and the direction of the first move is not reliable information (spec section 6).
    for ev in risk.recent:
        if ev.impact != "high":
            continue
        contrib = _WEIGHT["high"] * _proximity(ev.minutes_away, w.lock_after_minutes or 1)
        score = max(score, contrib)
        drivers.append(f"{ev.currency} {ev.title} released {abs(ev.minutes_away):.0f}m ago")
        break

    # 3. Which leg is exposed. Both legs having events inside the warning window is genuinely worse
    #    than one: the pair can be hit from either side and they can cancel or compound.
    near = {_ccy(e.country) for e in upcoming
            if e.importance in ("high", "medium")
            and (e.when - now).total_seconds() / 60.0 <= w.warning_minutes}
    near |= {_ccy(e.country) for e in just_out}
    risk.affects_base = bool(base and base.upper() in near)
    risk.affects_quote = bool(quote and quote.upper() in near)
    risk.both_sides = risk.affects_base and risk.affects_quote
    if risk.both_sides:
        score += _BOTH_SIDES_BONUS
        drivers.append("both legs have events nearby")

    # 4. The lock window is a FLOOR, not an addition: inside it the answer is wait regardless of
    #    how the ramp happened to add up.
    in_lock = (
        (risk.next_high is not None and risk.next_high.minutes_away <= w.lock_before_minutes)
        or any(e.impact == "high" and abs(e.minutes_away) <= w.lock_after_minutes
               for e in risk.recent)
    )
    if in_lock:
        score = max(score, float(w.wait_score))

    risk.score = int(max(0, min(100, round(score))))
    risk.band = band_for(risk.score)
    if risk.score >= w.wait_score or in_lock:
        risk.action = "wait"
    elif risk.score >= w.warn_score:
        risk.action = "warn"
    else:
        risk.action = "proceed"
    risk.reason = "; ".join(drivers) if drivers else "no high or medium impact events nearby"
    if risk.stale:
        risk.note = "calendar data is stale - treat the timing read with caution"
    return risk
