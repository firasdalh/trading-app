"""Per-symbol scorecard — the system grading its own results, symbol by symbol.

Everything else in this app decides BEFORE a trade. This is the only part that looks at what
actually happened afterwards and feeds it back. It exists because the most valuable findings in this
book were all discovered by hand, weeks late: forex was right 45 times per 100 (worse than a coin)
across 337 signals before anyone noticed, while the data to see it had been sitting in the journal
the whole time.

WHY THIS CAN'T OVERFIT THE WAY AN ENTRY FILTER CAN
    An entry filter says "given the past, this rule should help" — a prediction, fitted to history,
    and four such rules in a row measured better in-sample and worse out-of-sample. This module
    predicts nothing. It counts closed trades that already happened and reports the arithmetic. The
    only judgement is "is this sample big enough and far enough from zero to be more than luck",
    which is a statistics question, not a market one.

THE MEASURE
    Classification uses EXPECTANCY IN R (average profit per unit of risk), not win rate. A symbol
    winning 35% with 3R winners is excellent; one winning 60% with 0.3R winners is a slow loss. Win
    rate is reported alongside because it is the intuitive number, but it must not be the verdict.

THE STATISTICS
    A symbol is only judged once it has ``min_trades`` closed trades AND its mean R is far enough
    from zero that chance is an unlikely explanation (one-sided ~95%, mean ± 1.64 standard errors).
    Anything else stays WATCHING. This is deliberately slow to condemn: disabling a good symbol on a
    losing streak is a worse error than carrying a bad one for another week.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.db import Position, WatchItem
from app.models.enums import PositionStatus

log = get_logger("risk.scorecard")

# One-sided ~95% confidence. The bar for acting on a symbol, in standard errors from zero.
_Z = 1.64

# Verdicts, worst to best.
DISABLE = "disable"     # significantly negative -> stop trading it (warn, or auto-disable if on)
WEAK = "weak"           # negative but not yet proven so -> watch closely
WATCHING = "watching"   # judged, but indistinguishable from zero either way
PROVEN = "proven"       # significantly positive -> this is where the edge is
LEARNING = "learning"   # not enough closed trades to say anything


@dataclass
class SymbolScore:
    symbol: str
    asset_class: str | None
    trades: int
    wins: int
    win_rate: float          # % — the intuitive number, NOT the verdict
    expectancy_r: float      # mean R per trade — what the verdict is based on
    total_r: float
    total_pnl: float
    verdict: str
    reason: str
    enabled: bool = True
    significant: bool = False  # is the result distinguishable from luck?


@dataclass
class Scorecard:
    scores: list[SymbolScore] = field(default_factory=list)
    min_trades: int = 30
    auto_disable: bool = False
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def to_disable(self) -> list[SymbolScore]:
        """Symbols the evidence says to stop trading (and that are still switched on)."""
        return [s for s in self.scores if s.verdict == DISABLE and s.enabled]


def _classify(n: int, mean_r: float, se: float, min_trades: int) -> tuple[str, str, bool]:
    """(verdict, reason, significant) from the sample size and how far the mean sits from zero."""
    if n < min_trades:
        return LEARNING, f"only {n} of {min_trades} trades needed before judging", False
    if se <= 0:
        # Zero spread means every trade returned the SAME R — perfect consistency, which is the
        # strongest evidence available, not the weakest. Only a run that is also exactly flat
        # carries no information.
        if mean_r < 0:
            return DISABLE, f"lost {abs(mean_r):.2f}R on every one of {n} trades", True
        if mean_r > 0:
            return PROVEN, f"made {mean_r:+.2f}R on every one of {n} trades", True
        return WATCHING, f"all {n} trades finished exactly flat", False
    lo, hi = mean_r - _Z * se, mean_r + _Z * se   # ~95% one-sided bounds
    if hi < 0:
        return (DISABLE,
                f"lost {abs(mean_r):.2f}R per trade over {n} trades — worse than break-even by more "
                "than luck explains", True)
    if lo > 0:
        return (PROVEN,
                f"made {mean_r:+.2f}R per trade over {n} trades — a real edge, not luck", True)
    if mean_r < 0:
        return (WEAK,
                f"losing {abs(mean_r):.2f}R per trade over {n} trades, but still inside the range "
                "luck could explain — watching", False)
    return WATCHING, f"{mean_r:+.2f}R per trade over {n} trades — too close to zero to call", False


def build_scorecard(session: Session, *, min_trades: int = 30,
                    auto_disable: bool = False, limit_days: int | None = None) -> Scorecard:
    """Grade every symbol that has closed trades. Pure read — never changes anything."""
    q = select(Position).where(
        Position.status == PositionStatus.CLOSED.value,
        Position.realized_pnl.is_not(None),
    )
    if limit_days:
        from datetime import timedelta
        q = q.where(Position.closed_at >= datetime.now(timezone.utc) - timedelta(days=limit_days))
    rows = list(session.scalars(q).all())

    enabled = {w.symbol: w.enabled for w in session.scalars(select(WatchItem)).all()}

    by_symbol: dict[str, list[Position]] = {}
    for p in rows:
        by_symbol.setdefault(p.symbol, []).append(p)

    scores: list[SymbolScore] = []
    for symbol, ps in by_symbol.items():
        # R per trade needs a recorded risk; trades without one can't be scored in R.
        rs = [p.realized_pnl / p.risk_amount for p in ps if p.risk_amount]
        n = len(rs)
        wins = sum(1 for p in ps if (p.realized_pnl or 0) > 0)
        pnl = sum(p.realized_pnl or 0.0 for p in ps)
        if n == 0:
            scores.append(SymbolScore(
                symbol=symbol, asset_class=ps[0].asset_class, trades=len(ps), wins=wins,
                win_rate=round(100.0 * wins / len(ps), 1), expectancy_r=0.0, total_r=0.0,
                total_pnl=round(pnl, 2), verdict=LEARNING,
                reason="no risk recorded on these trades — can't score them in R",
                enabled=enabled.get(symbol, True)))
            continue
        mean_r = sum(rs) / n
        var = sum((r - mean_r) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
        se = math.sqrt(var / n) if n > 1 else 0.0
        verdict, reason, sig = _classify(n, mean_r, se, min_trades)
        scores.append(SymbolScore(
            symbol=symbol, asset_class=ps[0].asset_class, trades=n, wins=wins,
            win_rate=round(100.0 * wins / len(ps), 1), expectancy_r=round(mean_r, 3),
            total_r=round(sum(rs), 2), total_pnl=round(pnl, 2),
            verdict=verdict, reason=reason, enabled=enabled.get(symbol, True), significant=sig))

    order = {DISABLE: 0, WEAK: 1, WATCHING: 2, LEARNING: 3, PROVEN: 4}
    scores.sort(key=lambda s: (order.get(s.verdict, 9), s.expectancy_r))
    return Scorecard(scores=scores, min_trades=min_trades, auto_disable=auto_disable)


def apply_scorecard(session: Session, card: Scorecard) -> list[str]:
    """Switch off the watchlist entries the scorecard condemned. Returns the symbols disabled.

    A no-op unless ``auto_disable`` is on — the default is to WARN only, because turning a symbol
    off is the user's call and a scorecard should not quietly shrink the watchlist behind them."""
    if not card.auto_disable:
        return []
    targets = {s.symbol for s in card.to_disable}
    if not targets:
        return []
    disabled: list[str] = []
    for w in session.scalars(select(WatchItem).where(WatchItem.symbol.in_(targets))).all():
        if w.enabled:
            w.enabled = False
            session.add(w)
            disabled.append(w.symbol)
    if disabled:
        session.commit()
        log.warning("scorecard auto-disabled symbols", extra={"symbols": disabled})
    return disabled
