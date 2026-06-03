"""Reflection / Journal agent (read-only).

Reviews the closed-trade log and surfaces patterns and lessons (e.g. "losers are held
longer than winners"). It is strictly READ-ONLY — it has no path to place or modify orders.
LLM-driven when a key is configured; otherwise a deterministic heuristic produces useful
observations from the trade statistics.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.llm import analyze, llm_available
from app.core.logging import get_logger
from app.models.db import AgentRun, Position
from app.models.enums import Direction, PositionStatus
from app.models.schemas import ReflectionInsights, ReflectionReport

log = get_logger("agents.reflection")

_SYSTEM = """You are a trading journal/reflection coach reviewing a log of CLOSED trades and
their summary statistics. Surface honest, specific patterns and concrete, actionable lessons
(e.g. position sizing, holding losers too long, over-trading one direction, trading into
news). Be candid but constructive. You do NOT and cannot place trades. Return strict JSON
matching the schema (summary, patterns, lessons)."""


def _closed_positions(session: Session, limit: int = 200) -> list[Position]:
    return list(session.scalars(
        select(Position)
        .where(Position.status == PositionStatus.CLOSED.value)
        .order_by(Position.closed_at.desc())
        .limit(limit)
    ).all())


def _hold_hours(p: Position) -> float:
    if not p.opened_at or not p.closed_at:
        return 0.0
    o = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=timezone.utc)
    c = p.closed_at if p.closed_at.tzinfo else p.closed_at.replace(tzinfo=timezone.utc)
    return round((c - o).total_seconds() / 3600.0, 2)


def _stats(trades: list[Position]) -> dict:
    pnls = [(t.realized_pnl or 0.0) for t in trades]
    wins = [t for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t for t in trades if (t.realized_pnl or 0) < 0]
    gross_win = sum(t.realized_pnl or 0 for t in wins)
    gross_loss = sum(t.realized_pnl or 0 for t in losses)
    return {
        "trades_reviewed": len(trades),
        "win_rate": round(len(wins) / len(trades), 3) if trades else 0.0,
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_win / abs(gross_loss), 3) if gross_loss != 0 else None,
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "avg_hold_win": round(sum(_hold_hours(t) for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_hold_loss": round(sum(_hold_hours(t) for t in losses) / len(losses), 2) if losses else 0.0,
        "long_count": sum(1 for t in trades if t.direction == Direction.LONG.value),
        "short_count": sum(1 for t in trades if t.direction == Direction.SHORT.value),
    }


def _deterministic_insights(stats: dict) -> ReflectionInsights:
    patterns: list[str] = []
    lessons: list[str] = []
    n = stats["trades_reviewed"]
    if n == 0:
        return ReflectionInsights(summary="No closed trades yet — nothing to reflect on.",
                                  patterns=[], lessons=[])

    patterns.append(f"{n} closed trades, win rate {stats['win_rate']*100:.0f}%, net "
                    f"{stats['net_pnl']:+.2f}.")
    if stats["long_count"] != stats["short_count"]:
        side = "long" if stats["long_count"] > stats["short_count"] else "short"
        patterns.append(f"Direction skew: more {side} trades "
                        f"({stats['long_count']}L / {stats['short_count']}S).")
    if stats["avg_hold_loss"] > stats["avg_hold_win"] and stats["losses"]:
        patterns.append(f"Losers are held longer than winners "
                        f"({stats['avg_hold_loss']}h vs {stats['avg_hold_win']}h).")
        lessons.append("Consider cutting losers sooner — holding them longer is a common drag.")
    if stats["profit_factor"] is not None and stats["profit_factor"] < 1:
        lessons.append("Profit factor < 1: losses outweigh wins. Re-check entry quality and R:R.")
    if stats["win_rate"] < 0.4 and n >= 5:
        lessons.append("Low win rate — make sure average winners are large enough to compensate.")
    if not lessons:
        lessons.append("No strong negative pattern detected. Keep sample size growing before "
                       "drawing conclusions.")

    summary = (f"Reviewed {n} trades. Win rate {stats['win_rate']*100:.0f}%, "
               f"profit factor {stats['profit_factor']}. Net {stats['net_pnl']:+.2f}.")
    return ReflectionInsights(summary=summary, patterns=patterns, lessons=lessons)


def run_reflection(session: Session, limit: int = 200) -> ReflectionReport:
    trades = _closed_positions(session, limit)
    stats = _stats(trades)

    insights: ReflectionInsights | None = None
    if llm_available() and trades:
        rows = "\n".join(
            f"{t.symbol} {t.direction} qty={t.qty} entry={t.entry_price} exit={t.last_price} "
            f"pnl={t.realized_pnl} hold_h={_hold_hours(t)}"
            for t in trades[:100]
        )
        user = (f"STATS: {stats}\n\nCLOSED TRADES:\n{rows}\n\n"
                "Identify patterns and give concrete lessons.")
        insights = analyze(system=_SYSTEM, user=user, schema=ReflectionInsights, max_tokens=1500)

    if insights is None:
        insights = _deterministic_insights(stats)

    report = ReflectionReport(
        generated_at=datetime.now(timezone.utc),
        trades_reviewed=stats["trades_reviewed"],
        win_rate=stats["win_rate"],
        net_pnl=stats["net_pnl"],
        profit_factor=stats["profit_factor"],
        summary=insights.summary,
        patterns=insights.patterns,
        lessons=insights.lessons,
    )

    # Persist as an AgentRun for the journal history (read-only record).
    session.add(AgentRun(agent="reflection", event="report", detail=report.model_dump(mode="json")))
    session.commit()
    log.info("reflection generated", extra={"trades": report.trades_reviewed})
    return report


def latest_reflection(session: Session) -> ReflectionReport | None:
    row = session.scalars(
        select(AgentRun).where(AgentRun.agent == "reflection", AgentRun.event == "report")
        .order_by(AgentRun.id.desc())
    ).first()
    if row and row.detail:
        return ReflectionReport.model_validate(row.detail)
    return None
