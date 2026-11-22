"""Journal / reflection routes (Milestone 9).

- GET  /api/journal/trades            -> recent CLOSED positions (the trade log), each with its session.
- GET  /api/journal/sessions          -> the same trades by the trading session they opened in, and by hour.
- GET  /api/journal/chart-trades      -> one pair's trades, entry + every exit, for the chart's history.
- POST /api/journal/reflect           -> run the read-only Reflection agent now.
- GET  /api/journal/reflection/latest -> the most recent stored reflection (or null).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.reflection import latest_reflection, run_reflection
from app.core.database import get_session
from app.core.sessions import SESSIONS, session_at, session_of, utc_hour
from app.core.state import get_or_create_risk_config, get_or_create_settings
from app.models.db import entry_risk
from app.models.db import Position
from app.models.enums import AssetClass, PositionStatus
from app.models.schemas import PositionView, ReflectionReport, ScorecardView

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
    # A trade with no score is left out: a strategy's trades carry confidence 0.0 (its rules don't score a setup),
    # and all 191 of them sat in "0-50%" as if the engine had scored them low (2026-11-11).
    conds = [Position.status == PositionStatus.CLOSED.value,
             Position.confidence.is_not(None), Position.confidence > 0, Position.realized_pnl.is_not(None),
             # Manual trades were stored with confidence 1.0 and filled the top bucket with
             # discretionary trades; they carry no engine score (full review 2026-10-20).
             (Position.source.is_(None)) | (~Position.source.like("manual%"))]
    settings = get_or_create_settings(session)
    # The account it trades now: a demo account's trades never mix into a live account's journal (2026-11-11).
    conds.append(Position.broker_env == settings.broker_env)
    if settings.journal_reset_at is not None:
        conds.append(Position.closed_at >= settings.journal_reset_at)
    rows = session.scalars(select(Position).where(*conds)).all()
    edges = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out: list[CalibrationBucket] = []
    for lo, hi in edges:
        b = [r for r in rows if lo <= (r.confidence or 0.0) < hi]
        n = len(b)
        wins = sum(1 for r in b if (r.realized_pnl or 0.0) > 0)
        rs = [(r.realized_pnl or 0.0) / entry_risk(r) for r in b if entry_risk(r)]
        out.append(CalibrationBucket(
            bucket=f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%",
            trades=n, wins=wins,
            win_rate=round(wins / n, 3) if n else None,
            avg_r=round(sum(rs) / len(rs), 2) if rs else None,
        ))
    return out


# A broker trade and the app's own row for it: the app records a position as its order fills, a second or two
# after the entry deal (252 of 252 real-account rows within 120 s, 2026-11-11).
_MATCH_S = 180


def _utc(dt: datetime | None) -> datetime | None:
    """A naive time is UTC already (how the DB keeps them)."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _family(src: str | None) -> str:
    """'manual_from_…' reads 'manual': a long source is cut short in the comment's 31 characters."""
    s = src or ""
    return "manual" if s.startswith("manual") else s


def _pair_app_rows(session: Session, trades: list[PositionView]) -> dict[int, Position]:
    """The app's own closed row for each broker trade it placed - {index in ``trades``: row}. Same symbol and side,
    entry within 0.5%, OPENED within _MATCH_S of it (closed, when either has no open time), and the source the
    trade's MT5 tag names, when it names one. Closest pairs first, each trade and each row once - who gets a row
    never hangs on the list's order. The row holds what the broker's history doesn't: the risk taken (the R
    denominator), and who opened a trade tagged before the comment named the source."""
    from app.risk.service import _norm_symbol

    rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.CLOSED.value,
                               Position.source.is_not(None))
    ).all()
    # Each row's values read once (2026-11-22): read off the ORM rows inside the trades x rows loop they cost
    # ~110k attribute reads and _family() calls - a quarter of a second on every Journal card's request.
    by_key: dict[tuple[str, str], list[tuple[int, str, float, datetime | None, datetime | None]]] = {}
    for i, p in enumerate(rows):
        by_key.setdefault((_norm_symbol(p.symbol), p.direction), []).append(
            (i, _family(p.source), p.entry_price or 0.0, _utc(p.opened_at), _utc(p.closed_at)))
    pairs = []
    for j, bt in enumerate(trades):
        tag = bt.source or "app"
        ftag = _family(tag)
        bent = bt.entry_price or 0.0
        be = max(abs(bent), 1e-9)
        bop, bcl = _utc(bt.opened_at), _utc(bt.closed_at)
        for i, fam, pent, pop, pcl in by_key.get((_norm_symbol(bt.symbol), bt.direction), ()):
            if tag != "app" and fam != ftag:
                continue                         # the broker's record says someone else opened it
            rel = abs(pent - bent) / be
            if rel > 0.005:
                continue
            pt, bt_t = pop, bop
            if not (pt and bt_t):
                pt, bt_t = pcl, bcl
            if not (pt and bt_t):
                continue
            dt = abs((pt - bt_t).total_seconds())
            if dt <= _MATCH_S:                   # further apart: another trade at a similar price, not this one
                pairs.append((dt, rel, j, i))
    out: dict[int, Position] = {}
    used: set[int] = set()
    for _dt, _rel, j, i in sorted(pairs):
        if j not in out and i not in used:
            out[j] = rows[i]
            used.add(i)
    return out


def _attribute_sources(session: Session, broker_trades: list[PositionView]) -> dict[int, Position]:
    """Who opened each broker trade. MT5's deal history has no source field, but every order the app sends
    carries "ai-trading-app:<source>" as its comment, which the adapter reads off the ENTRY deal
    (mt5_adapter.source_from_comment) - the broker's own record, from any machine on the account. It is kept:
    an untagged trade was placed by hand, in the terminal or on the phone, and stays 'manual' whatever the
    app's rows say.

    Only a trade the tag can't place - 'app' (a bare "ai-trading-app", from an app version that didn't write
    the source yet), or no source at all (a broker without tags) - takes the source of the app's own row for
    the SAME trade (_pair_app_rows). Returns those pairs.

    Until 2026-11-11 every trade was re-matched on symbol + side + entry price alone, however far apart in time,
    and the app's row won over the tag: 40 phone trades (-$1,602) read as strategy:1, borrowing rows from other
    days - many from the demo account, which a real account's history never holds."""
    pairs = _pair_app_rows(session, broker_trades)
    for j, row in pairs.items():
        if (broker_trades[j].source or "app") == "app":
            broker_trades[j].source = row.source
    return pairs


# The journal book is built once and shared by the cards for this long (2026-11-22, the user: "the Journal is slow"):
# its 7 cards each rebuilt it - the broker's 120-day history read and paired with the app's rows - on their own
# 8-30 s polls, all at once when the page opens. The rows are detached from the request that read them and only
# read after that (nothing here writes a journal row). A trade closed a moment ago shows within this.
_BOOK_TTL_S = 6.0
_BOOK_LOCK = threading.Lock()
_BOOK: dict = {}


def _forget_book() -> None:
    with _BOOK_LOCK:
        _BOOK.clear()


def _journal_book(session: Session) -> list[tuple[PositionView, Position | None]]:
    """_build_book, shared for _BOOK_TTL_S (one build at a time: the cards that ask together wait for it). Each
    caller gets its own copies of the trades."""
    key = str(get_or_create_settings(session).journal_reset_at)
    with _BOOK_LOCK:
        if not (_BOOK.get("key") == key and time.monotonic() - _BOOK.get("at", -1e9) < _BOOK_TTL_S):
            book = _build_book(session)
            for _v, row in book:
                if row is not None and row in session:
                    session.expunge(row)
            _BOOK.update(key=key, at=time.monotonic(), book=book)
        return [(v.model_copy(), row) for v, row in _BOOK["book"]]


def _build_book(session: Session) -> list[tuple[PositionView, Position | None]]:
    """Every closed trade of the journal, newest first, each tagged with the trading session it OPENED in
    (app.core.sessions), with the app's own row for it when the app placed it (None: a phone / terminal trade).

    Prefers the broker's own deal history (MT5): entry / exit / P&L / date match the Exness journal, and every
    trade on the account counts, the phone's too; the app's rows add who opened a legacy-tagged trade and the
    risk taken. Without that history: the app's own closed rows on the account it trades now
    (settings.broker_env) - a demo account's trades never mix into a live account's journal."""
    from app.risk.service import broker_closed_trades

    broker_trades = broker_closed_trades(session)  # already respects the journal reset marker
    if broker_trades is not None:
        pairs = _attribute_sources(session, broker_trades)
        book = [(bt, pairs.get(j)) for j, bt in enumerate(broker_trades)]
    else:
        settings = get_or_create_settings(session)
        conds = [Position.status == PositionStatus.CLOSED.value, Position.broker_env == settings.broker_env]
        if settings.journal_reset_at is not None:
            conds.append(Position.closed_at >= settings.journal_reset_at)
        rows = session.scalars(select(Position).where(*conds).order_by(Position.closed_at.desc())).all()
        book = [(PositionView.model_validate(r), r) for r in rows]
    for v, _row in book:
        v.session = session_of(v.opened_at)
    return book


def _view_book(session: Session, hide_manual: bool = False) -> list[tuple[PositionView, Position | None]]:
    """What a Journal card shows: _journal_book, without the trades opened by hand when the page asks
    (``hide_manual``). The user, 2026-11-18, after a big manual BTC loss: "remove the manual history ... I don't want
    to see this loss in my journal". A view only, kept per browser on the page: nothing is deleted, and the broker's
    history, the risk limits, the header's DAY P&L, the strategy's learner and the desk's own checks (loss run, the
    scalp ticket's day) always read every trade. Manual = the 'manual' family: the desk's ticket and an untagged
    trade from the phone or the terminal."""
    book = _journal_book(session)
    return [(v, r) for v, r in book if _family(v.source) != "manual"] if hide_manual else book


def _journal_trades(session: Session, limit: int | None = None, hide_manual: bool = False) -> list[PositionView]:
    """The journal's closed trades (_view_book), newest first - the newest ``limit``."""
    trades = [v for v, _row in _view_book(session, hide_manual)]
    return trades if limit is None else trades[:limit]

class EquityTrade(BaseModel):
    closed_at: datetime
    opened_at: datetime | None = None
    pnl: float
    source: str
    symbol: str
    direction: str
    r: float | None = None


@router.get("/equity", response_model=list[EquityTrade])
def equity(session: Session = Depends(get_session), hide_manual: bool = False) -> list[EquityTrade]:
    """Every closed trade of the journal (_journal_book), OLDEST first, in a few fields: what the Journal's
    performance charts draw (2026-11-11, "enhance the performance visualization") - the equity curve and its
    drawdown, each day's P&L and the spread of the results, for the whole account or one source. The broker's $
    (swap and commission in), the source MT5 recorded, R where the app's own row holds the risk taken."""
    out: list[EquityTrade] = []
    for v, row in _view_book(session, hide_manual):
        if v.realized_pnl is None or v.closed_at is None:
            continue
        risk = entry_risk(row) if row is not None else None
        out.append(EquityTrade(closed_at=_utc(v.closed_at), opened_at=_utc(v.opened_at), pnl=round(v.realized_pnl, 2),
                               source=v.source or "unknown", symbol=v.symbol, direction=str(v.direction),
                               r=round(v.realized_pnl / risk, 2) if risk else None))
    out.sort(key=lambda t: t.closed_at)
    return out


@router.get("/trades", response_model=list[PositionView])
def closed_trades(
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    hide_manual: bool = False,
) -> list[PositionView]:
    """Closed-trade log: the newest ``limit`` of the journal's trades (_journal_trades)."""
    return _journal_trades(session, limit, hide_manual)


@router.get("/chart-trades")
def chart_trades(
    symbol: str = Query(..., min_length=1, max_length=32),
    asset_class: AssetClass = Query(AssetClass.FOREX),
    days: int = Query(7, ge=1, le=120),
    session: Session = Depends(get_session),
) -> list[dict]:
    """One pair's past trades for the chart's MT5-style history: where each opened and where it closed
    (every exit - a partial close too - and why), from the broker's own deals. The journal's "start
    fresh" marker doesn't apply: the chart shows what happened on the pair, as MT5 does. One day more
    is read than asked, so a trade opened just before the chart's first bar keeps its entry. A broker
    with no deal history (the simulator) falls back to the app's own closed positions."""
    from app.brokers.registry import get_broker_for

    since = datetime.now(timezone.utc) - timedelta(days=days + 1)
    try:
        broker = get_broker_for(asset_class, get_or_create_settings(session).broker_map or {})
        rows = broker.get_trade_history(symbol, since)
    except Exception:  # noqa: BLE001 - no broker right now: the app's own record still draws
        rows = None
    if rows is not None:
        return rows

    def utc(dt: datetime | None) -> datetime | None:
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    out: list[dict] = []
    for p in session.scalars(
        select(Position).where(Position.status == PositionStatus.CLOSED.value,
                               func.upper(Position.symbol) == symbol.upper()).order_by(Position.opened_at)
    ).all():
        opened, closed = utc(p.opened_at), utc(p.closed_at)
        if closed is None or closed < since or p.last_price is None:
            continue
        out.append({
            "id": p.id, "direction": p.direction, "volume": p.qty, "entry_price": p.entry_price,
            "opened_at": opened.isoformat() if opened else None,
            "exits": [{"price": p.last_price, "at": closed.isoformat(), "volume": p.qty, "pnl": p.realized_pnl,
                       "by": None}],
            "pnl": p.realized_pnl, "open": False, "source": p.source,
        })
    return out


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
def stats(session: Session = Depends(get_session), hide_manual: bool = False) -> JournalStats:
    """Expectancy + R-multiple performance over the journal's closed trades that carry a recorded risk: the ones
    the app placed on this account (its own row holds the risk taken; a phone or terminal trade has none). The $
    are the broker's, swap and commission in. Answers 'what's the edge, in R?' and 'how deep was the worst
    drawdown?' — the numbers a desk reviews. Complements /calibration (which buckets the app's trades by
    confidence). Until 2026-11-11 it read every app row: 227 of its 480 trades were the demo account's."""
    from types import SimpleNamespace

    rows = [SimpleNamespace(realized_pnl=v.realized_pnl, closed_at=_utc(v.closed_at), risk_amount=entry_risk(r))
            for v, r in _view_book(session, hide_manual)
            if r is not None and v.realized_pnl is not None and entry_risk(r)]
    rows.sort(key=lambda x: x.closed_at or datetime.min.replace(tzinfo=timezone.utc))   # oldest first: the R curve
    n = len(rows)
    if n == 0:
        return JournalStats(trades=0, wins=0, losses=0, win_rate=None, expectancy_r=None,
                            avg_win_r=None, avg_loss_r=None, profit_factor=None,
                            total_r=None, max_drawdown_r=None)

    rs = [(r.realized_pnl or 0.0) / entry_risk(r) for r in rows]
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
    # The same per pair, for each source's trades alone - "By pair ... I want it also for the strategy" (2026-11-11).
    by_pair_source: dict[str, list[GroupStat]] = {}
    daily: list[PeriodBreakdown]
    weekly: list[PeriodBreakdown]
    monthly: list[PeriodBreakdown]


def _stat(label: str, rows: list) -> GroupStat:
    n = len(rows)
    wins = sum(1 for r in rows if (r.realized_pnl or 0.0) > 0)
    losses = sum(1 for r in rows if (r.realized_pnl or 0.0) < 0)
    net = sum((r.realized_pnl or 0.0) for r in rows)
    rs = [(r.realized_pnl or 0.0) / entry_risk(r) for r in rows if entry_risk(r)]
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
def breakdown(session: Session = Depends(get_session), hide_manual: bool = False) -> JournalBreakdown:
    """Closed-trade performance broken down by SOURCE (who opened it) and PAIR, plus a daily/weekly/
    monthly time split - over the journal's trades (_journal_book): the broker's history, the phone's trades in,
    each with the source MT5 recorded; R where the app's own row holds the risk taken. Until 2026-11-11 it read
    the app's rows only: a demo account's trades in, the phone's out."""
    from types import SimpleNamespace

    rows = [SimpleNamespace(realized_pnl=v.realized_pnl, source=v.source, symbol=v.symbol, closed_at=_utc(v.closed_at),
                            risk_amount=entry_risk(r) if r is not None else None)
            for v, r in _view_book(session, hide_manual) if v.realized_pnl is not None and v.closed_at is not None]

    def _iso_week(r) -> str:
        y, w, _ = r.closed_at.isocalendar()
        return f"{y}-W{w:02d}"

    return JournalBreakdown(
        by_source=_group(rows, lambda r: r.source or "unknown"),
        by_pair=_group(rows, lambda r: r.symbol),
        by_pair_source={src: _group([r for r in rows if (r.source or "unknown") == src], lambda r: r.symbol)
                        for src in {r.source or "unknown" for r in rows}},
        daily=_periods(rows, lambda r: r.closed_at.strftime("%Y-%m-%d"), 60),
        weekly=_periods(rows, _iso_week, 26),
        monthly=_periods(rows, lambda r: r.closed_at.strftime("%Y-%m"), 24),
    )


# --------------------------------------------------------------------------- #
#  Sessions: the log's closed trades by the trading session each one OPENED in
#  (app.core.sessions) and by the hour - which part of the day pays (2026-11-10).
# --------------------------------------------------------------------------- #

class SessionStat(BaseModel):
    trades: int
    wins: int
    losses: int
    win_rate: float | None        # fraction 0-1
    net_pnl: float                # $
    avg_pnl: float | None         # $ a trade
    profit_factor: float | None   # gross win / gross loss; None without a losing trade


class SourceStat(BaseModel):
    source: str                   # who opened them: strategy:1 / manual / ...
    stat: SessionStat


class SessionRow(BaseModel):
    key: str
    name: str
    start_utc: int                # hour, inclusive
    end_utc: int                  # hour, exclusive
    about: str
    total: SessionStat
    sources: list[SourceStat]     # the same trades split by who opened them, the most trades first


class HourRow(BaseModel):
    hour_utc: int
    session: str
    total: SessionStat
    sources: list[SourceStat]


class SessionsView(BaseModel):
    trades: int                   # counted: closed, with a booked result and an open time
    skipped: int                  # closed trades left out for want of either
    since: datetime | None        # the open of the oldest one counted
    sessions: list[SessionRow]    # in the day's order: Asia, Middle East, London, New York, between
    hours: list[HourRow]          # every UTC hour, 0-23


def _session_stat(rows: list[PositionView]) -> SessionStat:
    pnl = [r.realized_pnl or 0.0 for r in rows]
    n = len(pnl)
    win, loss = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    return SessionStat(
        trades=n, wins=len(win), losses=len(loss),
        win_rate=round(len(win) / n, 3) if n else None,
        net_pnl=round(sum(pnl), 2),
        avg_pnl=round(sum(pnl) / n, 2) if n else None,
        profit_factor=round(sum(win) / -sum(loss), 2) if loss else None,
    )


def _source_split(rows: list[PositionView]) -> list[SourceStat]:
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for r in rows:
        buckets[r.source or "unknown"].append(r)
    return [SourceStat(source=k, stat=_session_stat(v))
            for k, v in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


@router.get("/sessions", response_model=SessionsView)
def by_session(session: Session = Depends(get_session), hide_manual: bool = False) -> SessionsView:
    """Which session pays: the trade log's own closed trades (broker truth, the journal's window) grouped by
    the trading session each one OPENED in, and by its UTC hour, each split by who opened them. In $, as the
    log - the broker's rows carry no risk to read R from."""
    rows = _journal_trades(session, hide_manual=hide_manual)
    counted = [r for r in rows if r.opened_at is not None and r.realized_pnl is not None]
    hours: dict[int, list[PositionView]] = {h: [] for h in range(24)}
    for r in counted:
        hours[utc_hour(r.opened_at)].append(r)
    opens = [r.opened_at if r.opened_at.tzinfo else r.opened_at.replace(tzinfo=timezone.utc) for r in counted]
    return SessionsView(
        trades=len(counted), skipped=len(rows) - len(counted), since=min(opens) if opens else None,
        sessions=[SessionRow(key=s["key"], name=s["name"], start_utc=s["start"], end_utc=s["end"], about=s["about"],
                             total=_session_stat(rs), sources=_source_split(rs))
                  for s in SESSIONS for rs in [[r for r in counted if r.session == s["key"]]]],
        hours=[HourRow(hour_utc=h, session=session_at(h), total=_session_stat(rs), sources=_source_split(rs))
               for h, rs in hours.items()],
    )


class JournalResetView(BaseModel):
    journal_reset_at: datetime | None


@router.post("/reset", response_model=JournalResetView)
def reset_journal(session: Session = Depends(get_session)) -> JournalResetView:
    """Start a FRESH journal from now: the trade log, stats, and calibration then only count trades
    closed at/after this moment. Non-destructive — the broker's full deal history is untouched; call
    DELETE /journal/reset to show everything again."""
    _forget_book()
    settings = get_or_create_settings(session)
    settings.journal_reset_at = datetime.now(timezone.utc)
    session.commit()
    return JournalResetView(journal_reset_at=settings.journal_reset_at)


@router.delete("/reset", response_model=JournalResetView)
def clear_journal_reset(session: Session = Depends(get_session)) -> JournalResetView:
    """Undo the reset marker — show the full trade history again (nothing was ever deleted)."""
    _forget_book()
    settings = get_or_create_settings(session)
    settings.journal_reset_at = None
    session.commit()
    return JournalResetView(journal_reset_at=None)


class BackfillResult(BaseModel):
    sources_labelled: int   # legacy positions whose NULL source was set (no longer "Unknown")
    pnl_recovered: int      # closed positions whose realized P&L was recovered from broker history
    pnl_corrected: int = 0  # closed positions whose stored P&L was not their own trade's - now the broker's


@router.post("/backfill", response_model=BackfillResult)
def backfill(session: Session = Depends(get_session)) -> BackfillResult:
    """Repair the journal's app-tracked rows:

    1. SOURCE — positions opened before source-tracking existed carry a NULL source and show as
       'Unknown'. They were all opened via the deterministic analysis pipeline (their proposal
       records recorded ``source='analysis'``), so we label them 'analysis'. NOT broker/terminal
       trades — the app only ever rows a position it opened itself.
    2. P&L — closed rows that closed broker-side never booked a $ result, so the stats + by-source
       breakdown dropped them. We recover realized P&L + exit from the broker's own deal history.
    3. P&L that is not the row's own (2026-11-11) — a row its own broker trade can be found for gets that trade's
       result where it holds another: a neighbour's, from the old price-only match, or the app's estimate.

    Idempotent: a second run changes nothing.
    """
    _forget_book()
    from app.execution.monitor import correct_realized_pnl_from_broker, fill_realized_pnl_from_broker

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
    # 3. P&L that is someone else's: the row's own broker trade's result.
    corrected = correct_realized_pnl_from_broker(session)
    if corrected:
        session.commit()
    return BackfillResult(sources_labelled=len(legacy), pnl_recovered=recovered, pnl_corrected=corrected)


@router.post("/reflect", response_model=ReflectionReport)
def reflect(session: Session = Depends(get_session), hide_manual: bool = False) -> ReflectionReport:
    """Run the read-only Reflection agent now - without the trades opened by hand when the page hides them."""
    return run_reflection(session, hide_manual=hide_manual)


@router.get("/scorecard", response_model=ScorecardView)
def scorecard(
    days: int | None = Query(None, ge=1, le=3650,
                             description="only score trades closed in the last N days"),
    apply: bool = Query(False, description="also act on the verdict (only does anything when "
                                           "auto-disable is turned on)"),
    session: Session = Depends(get_session),
) -> ScorecardView:
    """Per-symbol report card built from CLOSED trades — the system grading its own results.

    Read-only by default. ``apply=true`` switches off condemned symbols, but ONLY when
    ``scorecard_auto_disable`` is enabled; otherwise the verdict is a warning and the call is a
    no-op, so a stray request can never silently shrink the watchlist."""
    from app.risk.scorecard import apply_scorecard, build_scorecard

    cfg = get_or_create_risk_config(session)
    card = build_scorecard(
        session,
        min_trades=getattr(cfg, "scorecard_min_trades", 30),
        auto_disable=getattr(cfg, "scorecard_auto_disable", False),
        limit_days=days,
    )
    if apply:
        apply_scorecard(session, card)
        card = build_scorecard(                     # re-read so `enabled` reflects the change
            session, min_trades=card.min_trades, auto_disable=card.auto_disable, limit_days=days)
    return ScorecardView(
        scores=[s.__dict__ for s in card.scores],
        min_trades=card.min_trades,
        auto_disable=card.auto_disable,
        generated_at=card.generated_at,
        warnings=[f"{s.symbol}: {s.reason}" for s in card.to_disable],
    )


@router.get("/reflection/latest", response_model=ReflectionReport | None)
def reflection_latest(session: Session = Depends(get_session)) -> ReflectionReport | None:
    return latest_reflection(session)


@router.get("/review")
def daily_review(session: Session = Depends(get_session), hide_manual: bool = False) -> dict:
    """The day in one card: today's closed trades with their R, source and the trader's own reason
    and tags; today's totals and longest losing run; and avg R per tag over the whole journal (only
    once a tag has 10+ trades). The data to tell planned trades from impulsive ones was never
    captured before (full review 2026-10-20).

    The journal's trades (_journal_book) closed since 00:00 UTC - the broker's day, the one the header's DAY P&L
    counts: every trade on this account, the phone's too, in the broker's $ (swap and commission in). The note and
    tags are the app's own row's, R where that row holds the risk taken. Until 2026-11-11 it read the app's rows
    only: no phone trade, the app's estimate of each P&L, and the demo account's trades in the per-tag averages."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    book = [(v, row) for v, row in _view_book(session, hide_manual)
            if v.realized_pnl is not None and v.closed_at is not None]
    book.sort(key=lambda vr: _utc(vr[0].closed_at))                 # oldest first: the losing run

    def r_of(v, row):
        risk = entry_risk(row) if row is not None else None
        return round(v.realized_pnl / risk, 2) if risk else None

    def review_of(row) -> dict:
        return (row.review or {}) if row is not None else {}

    today = [(v, row) for v, row in book if _utc(v.closed_at) >= day_start]
    streak = longest = 0
    for v, _row in today:
        streak = streak + 1 if (v.realized_pnl or 0) < 0 else 0
        longest = max(longest, streak)
    tags: dict[str, list[float]] = {}
    for v, row in book:
        for t in (review_of(row).get("tags") or []):
            r = r_of(v, row)
            if r is not None:
                tags.setdefault(t, []).append(r)
    return {
        "date": day_start.date().isoformat(),
        "trades": [{"symbol": v.symbol, "direction": v.direction, "source": v.source,
                    "pnl": round(v.realized_pnl or 0.0, 2), "r": r_of(v, row),
                    "note": review_of(row).get("note"), "tags": review_of(row).get("tags") or [],
                    "closed_at": _utc(v.closed_at).isoformat()} for v, row in today],
        "net_pnl": round(sum(v.realized_pnl or 0.0 for v, _row in today), 2),
        "total_r": round(sum(r for v, row in today if (r := r_of(v, row)) is not None), 2),
        "longest_losing_run": longest,
        "by_tag": [{"tag": t, "trades": len(v), "avg_r": round(sum(v) / len(v), 2)}
                   for t, v in sorted(tags.items()) if len(v) >= 10],
        "tags_below_10": sorted(t for t, v in tags.items() if len(v) < 10),
    }
