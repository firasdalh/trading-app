"""Journal / reflection routes (Milestone 9).

- GET  /api/journal/trades            -> recent CLOSED positions (the trade log).
- POST /api/journal/reflect           -> run the read-only Reflection agent now.
- GET  /api/journal/reflection/latest -> the most recent stored reflection (or null).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.reflection import latest_reflection, run_reflection
from app.core.database import get_session
from app.core.state import get_or_create_settings
from app.models.db import Position
from app.models.enums import PositionStatus
from app.models.schemas import PositionView, ReflectionReport

router = APIRouter(prefix="/api/journal", tags=["journal"])


class CalibrationBucket(BaseModel):
    bucket: str               # confidence range, e.g. "70-80%"
    trades: int
    wins: int
    win_rate: float | None    # None when the bucket has no trades yet
    avg_r: float | None       # mean realized R (realized_pnl / risk_amount)


@router.get("/calibration", response_model=list[CalibrationBucket])
def calibration(session: Session = Depends(get_session)) -> list[CalibrationBucket]:
    """Per-confidence-bucket win rate + avg R over CLOSED app-tracked trades — i.e. does the
    engine's confidence actually predict outcomes? (Does "70%" win ~70%?) Use it to recalibrate
    the score and the Hybrid auto-open threshold once enough trades have accumulated."""
    conds = [Position.status == PositionStatus.CLOSED.value,
             Position.confidence.is_not(None), Position.realized_pnl.is_not(None)]
    reset = get_or_create_settings(session).journal_reset_at
    if reset is not None:
        conds.append(Position.closed_at >= reset)
    rows = session.scalars(select(Position).where(*conds)).all()
    edges = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out: list[CalibrationBucket] = []
    for lo, hi in edges:
        b = [r for r in rows if lo <= (r.confidence or 0.0) < hi]
        n = len(b)
        wins = sum(1 for r in b if (r.realized_pnl or 0.0) > 0)
        rs = [(r.realized_pnl or 0.0) / r.risk_amount for r in b if r.risk_amount]
        out.append(CalibrationBucket(
            bucket=f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%",
            trades=n, wins=wins,
            win_rate=round(wins / n, 3) if n else None,
            avg_r=round(sum(rs) / len(rs), 2) if rs else None,
        ))
    return out


def _attribute_sources(session: Session, broker_trades: list[PositionView]) -> None:
    """The broker's deal history has no 'source' (it doesn't know who opened a trade), so every row
    would read 'Unknown'. Attribute each broker trade to the app's own closed Position (which DOES
    carry the source) by normalized symbol + direction + entry (±0.5%) + nearest close time, one app
    row per broker trade. A broker trade left with no match is genuinely external — a trade opened
    directly in the MT5/Exness terminal — and correctly stays 'Unknown'."""
    from app.risk.service import _norm_symbol

    def _aware(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.CLOSED.value,
                               Position.source.is_not(None))
    ).all()
    used: set[int] = set()
    for bt in broker_trades:
        best_i, best_key = None, None
        for i, p in enumerate(rows):
            if i in used or p.direction != bt.direction:
                continue
            if _norm_symbol(p.symbol) != _norm_symbol(bt.symbol):
                continue
            be = abs(bt.entry_price or 0.0)
            rel = abs((p.entry_price or 0.0) - (bt.entry_price or 0.0)) / max(be, 1e-9)
            if rel > 0.005:
                continue
            pc, bc = _aware(p.closed_at), _aware(bt.closed_at)
            dt = abs((pc - bc).total_seconds()) if (pc and bc) else 1e12
            key = (rel, dt)
            if best_key is None or key < best_key:
                best_key, best_i = key, i
        if best_i is not None:
            bt.source = rows[best_i].source
            used.add(best_i)


@router.get("/trades", response_model=list[PositionView])
def closed_trades(
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[PositionView]:
    """Closed-trade log. Prefers the broker's own deal history (MT5) so entry/exit/P&L/date
    match the Exness journal exactly; falls back to app-tracked closed positions otherwise.
    The broker rows are attributed a source by matching back to the app's own positions."""
    from app.risk.service import broker_closed_trades

    broker_trades = broker_closed_trades(session)  # already respects the journal reset marker
    if broker_trades is not None:
        _attribute_sources(session, broker_trades)
        return broker_trades[:limit]

    conds = [Position.status == PositionStatus.CLOSED.value]
    reset = get_or_create_settings(session).journal_reset_at
    if reset is not None:
        conds.append(Position.closed_at >= reset)
    rows = session.scalars(
        select(Position).where(*conds).order_by(Position.closed_at.desc()).limit(limit)
    ).all()
    return [PositionView.model_validate(r) for r in rows]


class JournalStats(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate: float | None         # fraction 0-1
    expectancy_r: float | None     # mean realized R per trade — the edge, in R
    avg_win_r: float | None
    avg_loss_r: float | None        # negative
    profit_factor: float | None    # gross profit / gross loss ($)
    total_r: float | None
    max_drawdown_r: float | None   # worst peak-to-trough on the cumulative-R curve (>= 0)


@router.get("/stats", response_model=JournalStats)
def stats(session: Session = Depends(get_session)) -> JournalStats:
    """Expectancy + R-multiple performance over CLOSED app-tracked trades (those with a recorded
    risk_amount). Answers 'what's the edge, in R?' and 'how deep was the worst drawdown?' — the
    numbers a desk reviews. Complements /calibration (which buckets the same trades by confidence)."""
    conds = [Position.status == PositionStatus.CLOSED.value,
             Position.realized_pnl.is_not(None), Position.risk_amount.is_not(None)]
    reset = get_or_create_settings(session).journal_reset_at
    if reset is not None:
        conds.append(Position.closed_at >= reset)
    rows = session.scalars(
        select(Position).where(*conds).order_by(Position.closed_at.asc())
    ).all()
    rows = [r for r in rows if r.risk_amount]  # need risk_amount > 0 for an R-multiple
    n = len(rows)
    if n == 0:
        return JournalStats(trades=0, wins=0, losses=0, win_rate=None, expectancy_r=None,
                            avg_win_r=None, avg_loss_r=None, profit_factor=None,
                            total_r=None, max_drawdown_r=None)

    rs = [(r.realized_pnl or 0.0) / r.risk_amount for r in rows]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    gross_win = sum((r.realized_pnl or 0.0) for r in rows if (r.realized_pnl or 0.0) > 0)
    gross_loss = -sum((r.realized_pnl or 0.0) for r in rows if (r.realized_pnl or 0.0) < 0)

    cum = peak = max_dd = 0.0
    for x in rs:                         # drawdown on the cumulative-R equity curve
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return JournalStats(
        trades=n, wins=len(wins), losses=len(losses),
        win_rate=round(len(wins) / n, 3),
        expectancy_r=round(sum(rs) / n, 2),
        avg_win_r=round(sum(wins) / len(wins), 2) if wins else None,
        avg_loss_r=round(sum(losses) / len(losses), 2) if losses else None,
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        total_r=round(sum(rs), 2),
        max_drawdown_r=round(max_dd, 2),
    )


# --------------------------------------------------------------------------- #
#  Breakdown: win rate / count / P&L by SOURCE (who opened it) and by PAIR,
#  with a daily / weekly / monthly time split — so we can see which mechanism
#  (AI, RSI-Over, armed, hybrid, manual, deterministic) actually makes money.
# --------------------------------------------------------------------------- #

class GroupStat(BaseModel):
    label: str                 # the source, the pair symbol, or "all"
    trades: int
    wins: int
    losses: int
    win_rate: float | None     # fraction 0-1
    net_pnl: float             # sum of realized P&L ($)
    total_r: float | None      # sum of R-multiples (realized_pnl / risk_amount)
    avg_r: float | None        # mean R per trade


class PeriodBreakdown(BaseModel):
    period: str                # "2026-07-10" / "2026-W28" / "2026-07"
    total: GroupStat
    sources: list[GroupStat]   # split of that period by source


class JournalBreakdown(BaseModel):
    by_source: list[GroupStat]       # all-time, per source (the headline comparison)
    by_pair: list[GroupStat]         # all-time, per pair
    daily: list[PeriodBreakdown]
    weekly: list[PeriodBreakdown]
    monthly: list[PeriodBreakdown]


def _stat(label: str, rows: list) -> GroupStat:
    n = len(rows)
    wins = sum(1 for r in rows if (r.realized_pnl or 0.0) > 0)
    losses = sum(1 for r in rows if (r.realized_pnl or 0.0) < 0)
    net = sum((r.realized_pnl or 0.0) for r in rows)
    rs = [(r.realized_pnl or 0.0) / r.risk_amount for r in rows if r.risk_amount]
    return GroupStat(
        label=label, trades=n, wins=wins, losses=losses,
        win_rate=round(wins / n, 3) if n else None,
        net_pnl=round(net, 2),
        total_r=round(sum(rs), 2) if rs else None,
        avg_r=round(sum(rs) / len(rs), 2) if rs else None,
    )


def _group(rows: list, key) -> list[GroupStat]:
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[key(r)].append(r)
    stats = [_stat(k, v) for k, v in buckets.items()]
    return sorted(stats, key=lambda g: -g.net_pnl)


def _periods(rows: list, key, limit: int) -> list[PeriodBreakdown]:
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[key(r)].append(r)
    out: list[PeriodBreakdown] = []
    for period in sorted(buckets.keys(), reverse=True)[:limit]:
        prows = buckets[period]
        out.append(PeriodBreakdown(period=period, total=_stat("all", prows),
                                   sources=_group(prows, lambda r: r.source or "unknown")))
    return out


@router.get("/breakdown", response_model=JournalBreakdown)
def breakdown(session: Session = Depends(get_session)) -> JournalBreakdown:
    """Closed-trade performance broken down by SOURCE (who opened it) and PAIR, plus a daily/weekly/
    monthly time split. App-tracked positions only (they carry source + risk_amount)."""
    conds = [Position.status == PositionStatus.CLOSED.value, Position.realized_pnl.is_not(None)]
    reset = get_or_create_settings(session).journal_reset_at
    if reset is not None:
        conds.append(Position.closed_at >= reset)
    rows = [r for r in session.scalars(select(Position).where(*conds)).all() if r.closed_at is not None]

    def _iso_week(r) -> str:
        y, w, _ = r.closed_at.isocalendar()
        return f"{y}-W{w:02d}"

    return JournalBreakdown(
        by_source=_group(rows, lambda r: r.source or "unknown"),
        by_pair=_group(rows, lambda r: r.symbol),
        daily=_periods(rows, lambda r: r.closed_at.strftime("%Y-%m-%d"), 60),
        weekly=_periods(rows, _iso_week, 26),
        monthly=_periods(rows, lambda r: r.closed_at.strftime("%Y-%m"), 24),
    )


class JournalResetView(BaseModel):
    journal_reset_at: datetime | None


@router.post("/reset", response_model=JournalResetView)
def reset_journal(session: Session = Depends(get_session)) -> JournalResetView:
    """Start a FRESH journal from now: the trade log, stats, and calibration then only count trades
    closed at/after this moment. Non-destructive — the broker's full deal history is untouched; call
    DELETE /journal/reset to show everything again."""
    settings = get_or_create_settings(session)
    settings.journal_reset_at = datetime.now(timezone.utc)
    session.commit()
    return JournalResetView(journal_reset_at=settings.journal_reset_at)


@router.delete("/reset", response_model=JournalResetView)
def clear_journal_reset(session: Session = Depends(get_session)) -> JournalResetView:
    """Undo the reset marker — show the full trade history again (nothing was ever deleted)."""
    settings = get_or_create_settings(session)
    settings.journal_reset_at = None
    session.commit()
    return JournalResetView(journal_reset_at=None)


class BackfillResult(BaseModel):
    sources_labelled: int   # legacy positions whose NULL source was set (no longer "Unknown")
    pnl_recovered: int      # closed positions whose realized P&L was recovered from broker history


@router.post("/backfill", response_model=BackfillResult)
def backfill(session: Session = Depends(get_session)) -> BackfillResult:
    """Repair the journal's app-tracked rows:

    1. SOURCE — positions opened before source-tracking existed carry a NULL source and show as
       'Unknown'. They were all opened via the deterministic analysis pipeline (their proposal
       records recorded ``source='analysis'``), so we label them 'analysis'. NOT broker/terminal
       trades — the app only ever rows a position it opened itself.
    2. P&L — closed rows that closed broker-side never booked a $ result, so the stats + by-source
       breakdown dropped them. We recover realized P&L + exit from the broker's own deal history.

    Idempotent: only fills rows that are still missing the data.
    """
    from app.execution.monitor import fill_realized_pnl_from_broker

    # 1. Source: legacy NULL -> "analysis" (the recorded source of the analysis pipeline).
    legacy = session.scalars(select(Position).where(Position.source.is_(None))).all()
    for p in legacy:
        p.source = "analysis"
        session.add(p)
    if legacy:
        session.commit()

    # 2. P&L: recover for every closed app row still missing it (bounded by the broker's history).
    closed_missing = session.scalars(
        select(Position).where(Position.status == PositionStatus.CLOSED.value,
                               Position.realized_pnl.is_(None))
    ).all()
    recovered = fill_realized_pnl_from_broker(session, closed_missing)
    if recovered:
        session.commit()
    return BackfillResult(sources_labelled=len(legacy), pnl_recovered=recovered)


@router.post("/reflect", response_model=ReflectionReport)
def reflect(session: Session = Depends(get_session)) -> ReflectionReport:
    return run_reflection(session)


@router.get("/reflection/latest", response_model=ReflectionReport | None)
def reflection_latest(session: Session = Depends(get_session)) -> ReflectionReport | None:
    return latest_reflection(session)
