"""RSI-Over strategy scan.

One click: sweep ALL available pairs across the tradable asset classes (forex / metals / energy /
indices / crypto) on the chosen timeframe and STOP at the FIRST pair whose RSI is at an extreme AND
the EMA10 has confirmed the turn (see ``orchestrator._rsi_over_decision``): overbought (RSI >= 75) =>
SHORT, oversold (RSI <= 25) => LONG. The found setup is persisted + risk-sized and, under Mode A,
queued for the user's approval (auto-opened under Modes B/C).

The universe is the broker's own instrument list (MT5 lists visible/Market-Watch symbols first);
if the broker can't enumerate (e.g. the sim), it falls back to the enabled watchlist so the feature
still works in dev. Mechanical only (no LLM). Every hard safety gate still applies downstream
(kill-switch, live-confirm, daily-loss, exposure, anti-stacking, min lot, the price-drift guard) —
this only FINDS and STAGES the trade; the Risk Manager and the user (Mode A) remain final.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.pipeline import analyze_symbol, preview_symbol
from app.brokers.registry import get_broker_for
from app.core.logging import get_logger
from app.core.state import get_or_create_settings, kill_switch_active
from app.models.db import AgentRun, RsiOverConfig, TradeProposalRecord, WatchItem
from app.models.enums import AssetClass, Direction, ProposalStatus
from app.risk.service import ScanCache, _norm_symbol, live_broker_positions

log = get_logger("agents.rsi_over")


def get_or_create_rsi_over_config(session: Session) -> RsiOverConfig:
    cfg = session.get(RsiOverConfig, 1)
    if cfg is None:
        cfg = RsiOverConfig(id=1, enabled=False, interval_seconds=900, timeframe="1h", confirm=True)
        session.add(cfg)
        session.commit()
    return cfg


def rsi_over_tick(session: Session) -> dict:
    """Scheduler entrypoint for the auto-watch: respect the enabled flag + interval, then run one
    RSI-Over sweep (staging the first pair that confirms). A no-op when disabled or too soon. The
    sweep itself persists its snapshot (reason + candidates) to the config via done()."""
    cfg = get_or_create_rsi_over_config(session)
    if not cfg.enabled:
        return {"ran": False, "reason": "disabled"}
    now = datetime.now(timezone.utc)
    if cfg.last_run_at is not None:
        last = cfg.last_run_at if cfg.last_run_at.tzinfo else cfg.last_run_at.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cfg.interval_seconds:
            return {"ran": False, "reason": "interval not elapsed"}
    cfg.last_run_at = now
    session.add(cfg)
    session.commit()
    return run_rsi_over_scan(session, cfg.timeframe, confirm=cfg.confirm, macd=cfg.macd)

# Which asset classes to sweep (the user's tradable universe: FX / metals / energy / indices / crypto).
_SCAN_CLASSES = (AssetClass.FOREX, AssetClass.METAL, AssetClass.ENERGY, AssetClass.INDEX, AssetClass.CRYPTO)
# Cap per class so one click stays responsive. MT5 lists visible/Market-Watch symbols FIRST, so the
# user's active instruments are scanned before the long tail.
_MAX_PER_CLASS = 40


def _universe(session: Session) -> list[tuple[str, str]]:
    """(symbol, asset_class) pairs to scan — the broker's instrument list across _SCAN_CLASSES,
    de-duped. Falls back to the enabled watchlist when the broker can't enumerate (sim)."""
    settings = get_or_create_settings(session)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for ac in _SCAN_CLASSES:
        try:
            broker = get_broker_for(ac, settings.broker_map)
            syms = broker.list_symbols(ac) or []
        except Exception as exc:  # noqa: BLE001 - one asset class failing shouldn't kill the sweep
            log.warning("rsi-over list_symbols failed", extra={"asset_class": ac.value, "error": str(exc)})
            syms = []
        for s in syms[:_MAX_PER_CLASS]:
            key = _norm_symbol(s)
            if key not in seen:
                seen.add(key)
                out.append((s, ac.value))
    if not out:  # broker can't enumerate (e.g. sim) -> fall back to the enabled watchlist
        for it in session.scalars(select(WatchItem).where(WatchItem.enabled.is_(True))).all():
            key = _norm_symbol(it.symbol)
            if key not in seen:
                seen.add(key)
                out.append((it.symbol, it.asset_class))
    return out


def _entry_ind(proposal, timeframe: str) -> dict:
    """The entry-timeframe indicator dict from a proposal's technical read ({} if absent)."""
    tfs = getattr(proposal.technical, "timeframes", None) or []
    if not tfs:
        return {}
    t0 = next((x for x in tfs if x.timeframe == timeframe), tfs[0])
    return t0.indicators or {}


def _rsi_of(proposal, timeframe: str) -> float | None:
    return _entry_ind(proposal, timeframe).get("rsi14")


def _candidates(landscape: list[tuple[str, str, float, bool]]) -> dict:
    """Top pairs nearest each extreme, as structured (clickable) rows. Each landscape item is
    (symbol, asset_class, rsi, is_extreme) — is_extreme = RSI cleared the zone but EMA10 hadn't
    confirmed. Returns {"overbought": [...], "oversold": [...]} (highest / lowest RSI first)."""
    def rows(items):
        return [{"symbol": s, "asset_class": ac, "rsi": round(r, 1), "extreme": ext}
                for s, ac, r, ext in items]
    return {"overbought": rows(sorted(landscape, key=lambda x: -x[2])[:5]),
            "oversold": rows(sorted(landscape, key=lambda x: x[2])[:5])}


def _conf_label(confirm: bool, macd: bool) -> str:
    """Human label for the active confirmation(s): 'EMA10', 'MACD', 'EMA10/MACD', or 'extreme-only'."""
    parts = [p for p, on in (("EMA10", confirm), ("MACD", macd)) if on]
    return "/".join(parts) if parts else "extreme-only"


def _closest_text(cands: dict) -> str:
    """The short 'closest to the extremes' line embedded in the summary reason. '(in zone)' = RSI has
    reached the extreme but the confirmation named in the main reason hasn't fired yet."""
    def fmt(items):
        return ", ".join(f"{c['symbol']} {c['rsi']:.0f}{' (in zone)' if c['extreme'] else ''}"
                         for c in items[:3])
    if not cands["overbought"] and not cands["oversold"]:
        return ""
    return f" Closest overbought: {fmt(cands['overbought'])}. Closest oversold: {fmt(cands['oversold'])}."


def run_rsi_over_scan(session: Session, timeframe: str | None = None, confirm: bool = True,
                      macd: bool = False) -> dict:
    """Sweep the available universe for the first RSI-extreme reversal the Risk Manager approves, stage
    it (Mode A: queue for approval; Modes B/C: auto-open), and return a short summary. ``confirm`` =
    require the EMA10 close-through (strong); ``macd`` = also accept a MACD cross/divergence (early).
    With both, either confirms; with neither, the RSI extreme alone fires. ``timeframe`` None -> '1h'."""
    tf = timeframe or "1h"

    def done(reason: str, found: dict | None = None, scanned: int = 0, signals: int = 0,
             candidates: dict | None = None) -> dict:
        cands = candidates or {"overbought": [], "oversold": []}
        session.add(AgentRun(agent="rsi_over", event="scan",
                             detail={"found": found, "reason": reason, "scanned": scanned,
                                     "signals": signals, "timeframe": tf}))
        # Persist the snapshot on the singleton so the panel restores it on refresh (survives reload).
        cfg = get_or_create_rsi_over_config(session)
        cfg.last_result = reason
        cfg.last_scan_at = datetime.now(timezone.utc)
        cfg.last_scanned = scanned
        cfg.last_candidates = json.dumps(cands)
        session.add(cfg)
        session.commit()
        return {"ran": True, "found": found, "reason": reason, "scanned": scanned, "signals": signals,
                "candidates": cands}

    if kill_switch_active(session):
        return done("kill-switch active — scan halted")

    # Fetch the open book once; reuse it for anti-stacking + every per-pair risk check (ScanCache).
    try:
        open_positions = live_broker_positions(session)
    except Exception as exc:  # noqa: BLE001 - don't scan blindly if the broker is unreachable
        log.warning("rsi-over: broker positions failed", extra={"error": str(exc)})
        return done("could not read open positions")
    open_syms = {_norm_symbol(p.symbol) for p in open_positions}
    pending_syms = {
        _norm_symbol(r.symbol) for r in session.scalars(
            select(TradeProposalRecord).where(
                TradeProposalRecord.status == ProposalStatus.PENDING_APPROVAL.value))
    }
    cache = ScanCache(open_book=[(p.symbol, p.direction) for p in open_positions])

    universe = _universe(session)
    if not universe:
        return done("no instruments to scan (broker returned no symbols and the watchlist is empty)")

    scanned = 0
    signals = 0              # directional RSI-extreme + EMA10-confirmed reads seen
    blocked: list[str] = []  # signals the Risk Manager refused (exposure/correlation/room/min-lot)
    landscape: list[tuple[str, str, float, bool]] = []  # (symbol, asset_class, rsi, extreme-unconfirmed)
    for symbol, ac_str in universe:
        if _norm_symbol(symbol) in open_syms or _norm_symbol(symbol) in pending_syms:
            continue
        try:
            ac = AssetClass(ac_str)
            prop, dec = preview_symbol(session, symbol, ac, tf, use_llm=False, cache=cache,
                                       rsi_over=True, rsi_confirm=confirm, rsi_macd=macd)
        except Exception as exc:  # noqa: BLE001 - one bad pair shouldn't stop the sweep
            log.warning("rsi-over preview failed", extra={"symbol": symbol, "error": str(exc)})
            continue
        scanned += 1
        ind = _entry_ind(prop, tf)
        rsi = ind.get("rsi14")
        if prop.direction not in (Direction.LONG, Direction.SHORT):
            if rsi is not None:
                # NO_TRADE: is RSI already in the zone (so EMA10 is the only thing missing)?
                from app.agents.orchestrator import _RSI_OVER_OB, _RSI_OVER_OS
                extreme = rsi >= _RSI_OVER_OB or rsi <= _RSI_OVER_OS
                landscape.append((symbol, ac_str, rsi, extreme))
            continue
        signals += 1
        if not dec.approved:
            blocked.append(f"{symbol} ({dec.reason})")
            continue  # a real RSI signal but risk-blocked — keep looking for a tradeable one

        # First tradeable signal -> persist + risk-manage + stage (Mode A queues it for approval).
        res = analyze_symbol(session, symbol, ac, tf, use_llm=False, rsi_over=True,
                             rsi_confirm=confirm, rsi_macd=macd)
        rsi_val = _rsi_of(prop, tf)
        found = {"symbol": symbol, "asset_class": ac_str, "timeframe": tf,
                 "direction": prop.direction.value, "rsi": rsi_val,
                 "proposal_id": res.proposal_id, "status": res.status,
                 "approved": bool(res.risk.approved) if res.risk else None}
        log.warning("rsi-over found setup", extra=found)
        label = f"{symbol} {prop.direction.value.upper()}"
        return done(f"{label} — RSI {rsi_val:.0f}" if rsi_val is not None else label,
                    found=found, scanned=scanned, signals=signals)

    cands = _candidates(landscape)
    label = _conf_label(confirm, macd)
    if blocked:
        return done(f"found {signals} RSI signal(s) but the Risk Manager blocked them "
                    f"({'; '.join(blocked[:3])})", scanned=scanned, signals=signals, candidates=cands)
    base = (f"no tradeable RSI extreme across {scanned} pairs" if label == "extreme-only"
            else f"no RSI-extreme + {label}-confirmed setup across {scanned} pairs")
    return done(f"{base}.{_closest_text(cands)}", scanned=scanned, signals=signals, candidates=cands)
