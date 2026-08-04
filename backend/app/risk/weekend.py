"""Weekend-gap protection — the ONE risk a stop-loss cannot cover.

A stop is an instruction to exit at a price. It only works while the market is trading. When a
market closes for the weekend and reopens somewhere else entirely, price never passes through the
stop — it jumps over it, and the position exits at whatever the reopen prints.

This is not theoretical. UKOILm #301: opened Friday 20:45 with a 0.654 stop (risking $49), held over
the weekend, reopened Monday 5.43 lower. It closed at **-$434 = -8.9R** on a trade sized to lose 1R.
No other single event in the journal comes close, and no amount of entry-filter work prevents it.

Two independent guards, both deterministic (no LLM, no broker session API):

  * BLOCK  — refuse NEW entries in the hours before the weekend close, so nothing fresh is carried in
  * FLATTEN — close positions still open at the close, so nothing is carried in at all

Crypto is exempt from both: it trades 24/7, so there is no gap to protect against.

Session times are a fixed UTC rule rather than a broker lookup. MT5 exposes per-symbol sessions, but
they vary by instrument and DST, and a wrong/missing answer here would either strand positions or
provide no protection at all. A fixed, conservative Friday close is predictable, testable, and errs
toward closing slightly early — which costs a little upside and removes the tail.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Reference weekly close, UTC. Most FX/CFD venues stop around 21:00-22:00 UTC on Friday; Exness FX
# closes 21:00 UTC (20:00 in northern summer). 21:00 is the conservative choice — being an hour early
# on a summer Friday is harmless, being an hour late is exactly the exposure we're removing.
WEEKEND_CLOSE_WEEKDAY = 4        # Monday=0 ... Friday=4
WEEKEND_CLOSE_HOUR_UTC = 21

_CRYPTO = "crypto"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_to_weekend_close(now: datetime) -> float | None:
    """Hours until this week's Friday close, or None once it has already passed for the week.

    Returns None on Saturday/Sunday and after Friday's close — the market is already shut, so there
    is no window to act in (a closed market is handled by the market-hours checks elsewhere)."""
    now = _aware(now).astimezone(timezone.utc)
    if now.weekday() > WEEKEND_CLOSE_WEEKDAY:
        return None                                   # Saturday / Sunday — already shut
    days_ahead = WEEKEND_CLOSE_WEEKDAY - now.weekday()
    close = (now.replace(hour=WEEKEND_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0)
             + timedelta(days=days_ahead))
    if close <= now:
        return None                                   # Friday, already past the close
    return (close - now).total_seconds() / 3600.0


def in_weekend_window(now: datetime, hours_before: float, asset_class: str | None) -> bool:
    """True when ``now`` is inside the protected window before the weekly close for this asset.

    ``hours_before`` <= 0 disables the window entirely. Crypto never enters it (24/7, no gap)."""
    if hours_before <= 0:
        return False
    if (asset_class or "").lower() == _CRYPTO:
        return False
    left = hours_to_weekend_close(now)
    return left is not None and left <= hours_before


def weekend_block_reason(now: datetime, hours_before: float, asset_class: str | None) -> str | None:
    """Veto reason for a NEW entry inside the window, or None to allow it."""
    if not in_weekend_window(now, hours_before, asset_class):
        return None
    left = hours_to_weekend_close(now)
    return (f"weekend guard: {left:.1f}h to the Friday close — not opening a new position that would "
            f"be carried through the weekend gap (a stop can't fill while the market is shut)")
