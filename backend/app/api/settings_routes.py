"""Settings + execution-control routes (Milestone 6).

Covers the execution-mode switch (A/B/C), the paper↔live switch, the live-confirmation
phrase flow, risk-parameter updates (clamped to the RISK.md ceilings — never weakened
beyond them), the kill-switch flatten, a manual monitor trigger, and the DB-backed open
positions view.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import build_settings_response
from app.core.config import get_settings
from app.core.database import get_session
from app.core.logging import get_logger
from app.core.state import get_or_create_risk_config, get_or_create_settings
from app.execution.kill_switch import flatten_all
from app.execution.monitor import monitor_positions
from app.models.db import Position
from app.models.enums import AssetClass, ExecutionMode, PositionStatus
from app.models.schemas import PositionAdvice, PositionView, SettingsResponse

log = get_logger("api.settings")
router = APIRouter(prefix="/api", tags=["settings"])


class ModeRequest(BaseModel):
    mode: ExecutionMode
    confirm_phrase: str | None = None


class BrokerEnvRequest(BaseModel):
    env: str  # "paper" | "live"
    confirm_phrase: str | None = None


class LiveConfirmRequest(BaseModel):
    confirm_phrase: str


class Mt5ConnectRequest(BaseModel):
    # All optional: leave login/password blank to attach to the terminal's current account.
    login: int | None = None
    password: str | None = None
    server: str | None = None
    path: str | None = None


class LlmConfigRequest(BaseModel):
    provider: str                 # "anthropic" | "gemini"
    model: str | None = None
    api_key: str | None = None    # write-only; omit to keep existing


class BrokerMapRequest(BaseModel):
    # asset class -> broker name, e.g. {"forex": "mt5", "metal": "mt5"}
    broker_map: dict[str, str]


class RiskUpdateRequest(BaseModel):
    risk_per_trade: float | None = None
    max_open_positions: int | None = None
    max_daily_loss: float | None = None
    max_total_exposure: float | None = None
    per_pair_cooldown_minutes: int | None = None
    # Master on/off for the daily-loss circuit breaker (demo-account testing convenience).
    daily_loss_breaker_enabled: bool | None = None


def _check_phrase(phrase: str | None) -> None:
    expected = get_settings().live_confirm_phrase
    if not phrase or phrase.strip() != expected:
        raise HTTPException(status_code=403, detail="live confirmation phrase does not match")


@router.post("/settings/mode", response_model=SettingsResponse)
def set_mode(req: ModeRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    settings = get_or_create_settings(session)

    if req.mode == ExecutionMode.C_AUTO_LIVE:
        # Live auto-exec: require the typed phrase, switch to live, and record confirmation.
        _check_phrase(req.confirm_phrase)
        settings.broker_env = "live"
        settings.live_confirmed_at = datetime.now(timezone.utc)
        log.warning("execution mode -> C_AUTO_LIVE (live confirmed)")
    elif req.mode == ExecutionMode.B_AUTO_PAPER:
        # Mode B is paper-only by definition.
        settings.broker_env = "paper"
        log.warning("execution mode -> B_AUTO_PAPER (paper)")
    else:
        log.info("execution mode -> A_PROPOSE_APPROVE")

    settings.execution_mode = req.mode.value
    session.commit()
    return build_settings_response(session)


@router.post("/settings/broker-env", response_model=SettingsResponse)
def set_broker_env(req: BrokerEnvRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    if req.env not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="env must be 'paper' or 'live'")
    settings = get_or_create_settings(session)
    if req.env == "live":
        _check_phrase(req.confirm_phrase)
        settings.broker_env = "live"
        settings.live_confirmed_at = datetime.now(timezone.utc)
        log.warning("broker env -> LIVE (confirmed)")
    else:
        settings.broker_env = "paper"
        settings.live_confirmed_at = None
        # Demote Mode C to A when leaving live, so we never auto-trade unintentionally.
        if settings.execution_mode == ExecutionMode.C_AUTO_LIVE.value:
            settings.execution_mode = ExecutionMode.A_PROPOSE_APPROVE.value
        log.info("broker env -> paper")
    session.commit()
    return build_settings_response(session)


@router.post("/settings/live-confirm", response_model=SettingsResponse)
def live_confirm(req: LiveConfirmRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Re-confirm live trading for the current process (required after each restart)."""
    _check_phrase(req.confirm_phrase)
    settings = get_or_create_settings(session)
    settings.live_confirmed_at = datetime.now(timezone.utc)
    session.commit()
    log.warning("live trading re-confirmed for this session")
    return build_settings_response(session)


_VALID_BROKERS = {"sim", "alpaca", "ccxt", "oanda", "mt5"}
_VALID_ASSETS = {"stock", "crypto", "forex", "metal", "energy", "index"}


@router.post("/settings/broker-map", response_model=SettingsResponse)
def set_broker_map(req: BrokerMapRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Point asset classes at a broker (e.g. forex/metal -> 'mt5' for Exness).

    Merges into the existing map. Unknown asset classes or brokers are rejected.
    """
    settings = get_or_create_settings(session)
    merged = dict(settings.broker_map or {})
    for asset, broker in req.broker_map.items():
        if asset not in _VALID_ASSETS:
            raise HTTPException(status_code=400, detail=f"unknown asset class: {asset}")
        if broker not in _VALID_BROKERS:
            raise HTTPException(status_code=400, detail=f"unknown broker: {broker}")
        merged[asset] = broker
    settings.broker_map = merged
    session.commit()
    log.info("broker_map updated", extra={"broker_map": merged})
    return build_settings_response(session)


@router.post("/settings/risk", response_model=SettingsResponse)
def update_risk(req: RiskUpdateRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Update risk parameters. Hard ceilings from RISK.md are enforced server-side; values
    that would weaken protection beyond the ceiling are rejected (never silently clamped up)."""
    cfg = get_settings()
    risk = get_or_create_risk_config(session)

    if req.risk_per_trade is not None:
        if not (0 < req.risk_per_trade <= cfg.risk_per_trade_ceiling):
            raise HTTPException(
                status_code=400,
                detail=f"risk_per_trade must be in (0, {cfg.risk_per_trade_ceiling}] per RISK.md",
            )
        risk.risk_per_trade = req.risk_per_trade
    if req.max_open_positions is not None:
        if req.max_open_positions < 1:
            raise HTTPException(status_code=400, detail="max_open_positions must be >= 1")
        risk.max_open_positions = req.max_open_positions
    if req.max_daily_loss is not None:
        if not (0 < req.max_daily_loss <= 1):
            raise HTTPException(status_code=400, detail="max_daily_loss must be in (0, 1]")
        risk.max_daily_loss = req.max_daily_loss
    if req.max_total_exposure is not None:
        if not (0 < req.max_total_exposure <= 1):
            raise HTTPException(status_code=400, detail="max_total_exposure must be in (0, 1]")
        risk.max_total_exposure = req.max_total_exposure
    if req.per_pair_cooldown_minutes is not None:
        if req.per_pair_cooldown_minutes < 0:
            raise HTTPException(status_code=400, detail="per_pair_cooldown_minutes must be >= 0")
        risk.per_pair_cooldown_minutes = req.per_pair_cooldown_minutes

    if req.daily_loss_breaker_enabled is not None:
        risk.daily_loss_breaker_enabled = req.daily_loss_breaker_enabled
        if not req.daily_loss_breaker_enabled:
            # Removing a hard protection — log loudly, extra-loud on a live account (RISK.md).
            broker_env = get_or_create_settings(session).broker_env
            log.warning(
                "DAILY-LOSS CIRCUIT BREAKER DISABLED — no daily-loss auto-pause/veto until re-enabled",
                extra={"broker_env": broker_env, "live": broker_env == "live"},
            )
        else:
            log.warning("daily-loss circuit breaker re-enabled")

    session.commit()
    log.info("risk config updated")
    return build_settings_response(session)


def _try_mt5_connect() -> dict:
    """Attempt an MT5 connection and return a status dict (never raises)."""
    from app.brokers.base import BrokerError
    from app.brokers.mt5_adapter import Mt5BrokerAdapter

    try:
        broker = Mt5BrokerAdapter()
        acct = broker.get_account()
        return {"connected": True, "is_paper": broker.is_paper,
                "equity": acct.equity, "cash": acct.cash, "open_positions": acct.open_positions}
    except BrokerError as exc:
        return {"connected": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "error": str(exc)}


@router.get("/settings/mt5/status", tags=["broker"])
def mt5_status(session: Session = Depends(get_session)) -> dict:
    """Report whether MT5/Exness is configured and currently reachable. Password never returned."""
    from app.brokers.mt5_credentials import resolve_mt5_credentials
    from app.models.db import Mt5Credentials

    row = session.get(Mt5Credentials, 1)
    creds = resolve_mt5_credentials()
    configured = bool(creds["login"] or creds["server"] or creds["path"] or (row is not None))
    status = _try_mt5_connect()
    return {
        "configured": configured,
        "login": creds["login"] or None,
        "server": creds["server"] or None,
        **status,
    }


@router.post("/settings/mt5", tags=["broker"])
def connect_mt5(req: Mt5ConnectRequest, session: Session = Depends(get_session)) -> dict:
    """Save MT5/Exness connection details from the UI and attempt to connect.

    Leave login/password blank to attach to the account the running terminal is logged into.
    On success, forex + metals are routed to MT5 automatically.
    """
    from app.brokers.registry import reset_registry
    from app.models.db import Mt5Credentials

    row = session.get(Mt5Credentials, 1) or Mt5Credentials(id=1)
    row.login = req.login or 0
    row.server = (req.server or "").strip()
    row.path = (req.path or "").strip()
    if req.password is not None:  # only overwrite when provided
        row.password = req.password
    session.add(row)
    session.commit()

    # Drop cached adapters so the next resolution rebuilds with the new credentials.
    reset_registry()

    status = _try_mt5_connect()
    if status.get("connected"):
        settings = get_or_create_settings(session)
        merged = dict(settings.broker_map or {})
        merged.update({"forex": "mt5", "metal": "mt5"})
        settings.broker_map = merged
        session.commit()
        log.warning("MT5 connected via UI", extra={"login": row.login or None, "server": row.server})
    else:
        log.warning("MT5 connect attempt failed", extra={"error": status.get("error")})
    return status


@router.get("/settings/llm", tags=["settings"])
def llm_status(session: Session = Depends(get_session)) -> dict:
    """Active AI provider/model and whether a key is configured (key never returned)."""
    from app.agents.llm_config import resolve_llm_config

    cfg = resolve_llm_config()
    return {"provider": cfg.provider, "model": cfg.model, "available": cfg.available}


@router.post("/settings/llm", tags=["settings"])
def set_llm(req: LlmConfigRequest, session: Session = Depends(get_session)) -> dict:
    """Select the AI provider (Claude/Gemini) + model + key from the UI, then test it."""
    from app.agents import llm
    from app.agents.llm_config import resolve_llm_config
    from app.models.db import LlmConfig

    if req.provider not in ("anthropic", "gemini", "openai"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic', 'gemini', or 'openai'")
    row = session.get(LlmConfig, 1) or LlmConfig(id=1)
    row.provider = req.provider
    row.model = (req.model or "").strip() or None
    if req.api_key:  # only overwrite when provided
        row.api_key = req.api_key
    session.add(row)
    session.commit()

    tested_ok, error = True, None
    try:
        llm.probe()
    except Exception as exc:  # noqa: BLE001
        tested_ok, error = False, str(exc)
        log.warning("llm probe failed", extra={"provider": req.provider, "error": error})

    cfg = resolve_llm_config()
    return {"provider": cfg.provider, "model": cfg.model, "available": cfg.available,
            "tested_ok": tested_ok, "error": error}


@router.get("/positions", response_model=list[PositionView], tags=["positions"])
def db_positions(session: Session = Depends(get_session)) -> list[PositionView]:
    """Open positions from our own records (app-opened trades; used for exposure accounting)."""
    rows = session.scalars(
        select(Position).where(Position.status == PositionStatus.OPEN.value).order_by(Position.id.desc())
    ).all()
    return [PositionView.model_validate(r) for r in rows]


@router.get("/positions/live", response_model=list[PositionView], tags=["positions"])
def live_positions(session: Session = Depends(get_session)) -> list[PositionView]:
    """Open positions reported by the BROKER itself — the real account truth, including
    trades opened directly in MT5/Exness (not just app-opened ones)."""
    from app.risk.service import live_broker_positions

    out = live_broker_positions(session)
    for i, p in enumerate(out):
        p.id = i + 1
    return out


@router.get("/positions/advice", response_model=list[PositionAdvice], tags=["positions"])
def positions_advice(session: Session = Depends(get_session)) -> list[PositionAdvice]:
    """Management guidance for OPEN positions — protect a winner / cut a loser, with special
    attention to imminent high-impact events. Advisory only; the user acts via the table."""
    from app.agents.position_advisor import advise_positions

    return advise_positions(session)


class AdvisorConfigRequest(BaseModel):
    enabled: bool | None = None
    auto_execute: bool | None = None
    auto_reenter: bool | None = None
    interval_seconds: int | None = Field(None, ge=30, le=3600)


class AdvisorAction(BaseModel):
    symbol: str
    action: str
    kind: str | None = None        # close | protect | breakeven | trail
    stop: float | None = None
    ok: bool = False
    reason: str = ""
    intended: str | None = None
    error: str | None = None


class AdvisorView(BaseModel):
    enabled: bool
    auto_execute: bool
    auto_reenter: bool
    interval_seconds: int
    last_run_at: str | None = None
    advice: list[PositionAdvice]
    actions: list[AdvisorAction] = []


def _iso_utc(dt) -> str | None:
    """SQLite drops tzinfo, so stamp naive timestamps as UTC before serializing — otherwise the
    browser reads them as local time and the 'last check' age is off by the UTC offset."""
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _advisor_view(session: Session, actions: list[dict] | None = None) -> AdvisorView:
    from app.agents.position_advisor import advise_positions, get_or_create_advisor_config

    cfg = get_or_create_advisor_config(session)
    return AdvisorView(
        enabled=cfg.enabled, auto_execute=cfg.auto_execute, auto_reenter=cfg.auto_reenter,
        interval_seconds=cfg.interval_seconds, last_run_at=_iso_utc(cfg.last_run_at),
        advice=advise_positions(session),
        actions=[AdvisorAction(**{k: v for k, v in a.items() if k != "asset_class"})
                 for a in (actions or [])],
    )


@router.get("/positions/advisor", response_model=AdvisorView, tags=["positions"])
def advisor_state(session: Session = Depends(get_session)) -> AdvisorView:
    """Advisor auto-watch config plus the current advisories (one read for the panel)."""
    return _advisor_view(session)


@router.post("/positions/advisor/config", response_model=AdvisorView, tags=["positions"])
def advisor_set_config(req: AdvisorConfigRequest, session: Session = Depends(get_session)) -> AdvisorView:
    from app.agents.position_advisor import get_or_create_advisor_config

    cfg = get_or_create_advisor_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    if req.auto_execute is not None:
        cfg.auto_execute = req.auto_execute
    if req.auto_reenter is not None:
        cfg.auto_reenter = req.auto_reenter
    if req.interval_seconds is not None:
        cfg.interval_seconds = req.interval_seconds
    session.commit()
    log.warning("advisor config updated", extra={"enabled": cfg.enabled,
                "auto_execute": cfg.auto_execute, "auto_reenter": cfg.auto_reenter,
                "interval": cfg.interval_seconds})
    return _advisor_view(session)


@router.post("/positions/advisor/run", response_model=AdvisorView, tags=["positions"])
def advisor_run_now(session: Session = Depends(get_session)) -> AdvisorView:
    """Run the advisor immediately (the 'Run now' button); auto-executes if that toggle is on."""
    from app.agents.position_advisor import run_advisor

    out = run_advisor(session)
    return _advisor_view(session, actions=out.get("actions", []))


class AdvisorActivityItem(BaseModel):
    run_id: int
    seq: int
    at: str | None = None
    symbol: str
    action: str
    kind: str | None = None
    stop: float | None = None
    ok: bool = False
    reason: str = ""
    error: str | None = None


@router.get("/positions/advisor/activity", response_model=list[AdvisorActivityItem], tags=["positions"])
def advisor_activity(limit: int = 30, session: Session = Depends(get_session)) -> list[AdvisorActivityItem]:
    """Timeline of what the advisor actually DID (auto-executed / blocked / pending), newest
    first — pulled from the audit log so headless actions show up too."""
    from app.models.db import AgentRun

    rows = session.scalars(
        select(AgentRun).where(AgentRun.agent == "advisor", AgentRun.event == "check")
        .order_by(AgentRun.id.desc()).limit(60)
    ).all()
    items: list[AdvisorActivityItem] = []
    for r in rows:
        for i, a in enumerate(((r.detail or {}).get("actions") or [])):
            items.append(AdvisorActivityItem(
                run_id=r.id, seq=i, at=_iso_utc(r.created_at), symbol=a.get("symbol", ""),
                action=a.get("action", ""), kind=a.get("kind"), stop=a.get("stop"),
                ok=bool(a.get("ok")), reason=a.get("reason", ""), error=a.get("error"),
            ))
            if len(items) >= limit:
                return items
    return items


class LiveCloseRequest(BaseModel):
    symbol: str
    asset_class: AssetClass


class SlTpRequest(BaseModel):
    symbol: str
    asset_class: AssetClass
    stop_loss: float | None = None
    take_profit: float | None = None


@router.post("/positions/live/close", tags=["positions"])
def live_close(req: LiveCloseRequest, session: Session = Depends(get_session)) -> dict:
    """Close a broker position by symbol (works for trades opened in MT5 directly).

    Also reconciles any matching app-tracked DB position so the Monitor stops managing it.
    """
    from datetime import datetime, timezone

    from app.brokers.registry import get_broker_for

    settings = get_or_create_settings(session)
    broker = get_broker_for(req.asset_class, settings.broker_map)
    result = broker.close_position(req.symbol)

    # Reconcile DB-tracked positions for this symbol (book their last unrealized as realized).
    rows = session.scalars(
        select(Position).where(Position.symbol == req.symbol,
                               Position.status == PositionStatus.OPEN.value)
    ).all()
    if rows:
        daily = get_or_create_daily_state(session)
        for r in rows:
            r.status = PositionStatus.CLOSED.value
            r.closed_at = datetime.now(timezone.utc)
            r.realized_pnl = r.unrealized_pnl or 0.0
            daily.realized_pnl = round(daily.realized_pnl + (r.unrealized_pnl or 0.0), 2)
        session.commit()

    if result.status.value in ("error", "rejected"):
        raise HTTPException(status_code=409, detail=result.error or "broker close failed")
    return {"status": result.status.value, "symbol": req.symbol}


@router.post("/positions/{position_id}/close", tags=["positions"])
def close_position(position_id: int, session: Session = Depends(get_session)) -> dict:
    """Close a single open position (manual exit from the UI)."""
    from app.execution.monitor import close_one

    result = close_one(session, position_id)
    if not result.get("closed"):
        raise HTTPException(status_code=409, detail=result.get("error", "could not close"))
    return result


@router.post("/positions/sl-tp", tags=["positions"])
def set_position_sl_tp(req: SlTpRequest, session: Session = Depends(get_session)) -> dict:
    """Attach/modify SL and/or TP on an open position (e.g. protect a manual MT5 trade)."""
    from app.brokers.registry import get_broker_for

    settings = get_or_create_settings(session)
    broker = get_broker_for(req.asset_class, settings.broker_map)
    result = broker.set_sl_tp(req.symbol, req.stop_loss, req.take_profit)
    if result.status.value in ("error", "rejected"):
        raise HTTPException(status_code=409, detail=result.error or "could not set SL/TP")
    return {"status": result.status.value, "symbol": req.symbol,
            "stop_loss": req.stop_loss, "take_profit": req.take_profit}


@router.post("/execution/flatten", tags=["safety"])
def flatten(session: Session = Depends(get_session)) -> dict:
    """Close ALL open positions immediately (kill-switch flatten)."""
    return flatten_all(session)


@router.post("/execution/monitor", tags=["execution"])
def run_monitor(session: Session = Depends(get_session)) -> dict:
    """Run one position-monitor pass (also runs automatically on the scheduler)."""
    return monitor_positions(session)


# --------------------------------------------------------------------------- #
#  Hybrid auto-pilot
# --------------------------------------------------------------------------- #


class HybridConfigRequest(BaseModel):
    enabled: bool | None = None
    # Bounds match the documented Hybrid range in RISK.md (interval 30-90 min, confidence
    # 50-95%), so the API is the single source of truth — the UI clamp is merely cosmetic and a
    # direct POST can't make the auto-pilot more trigger-happy than documented.
    interval_seconds: int | None = Field(None, ge=1800, le=5400)  # 30-90 min
    min_confidence: float | None = Field(None, ge=0.5, le=0.95)   # 50-95%


class HybridView(BaseModel):
    enabled: bool
    interval_seconds: int
    min_confidence: float
    last_run_at: str | None = None
    last_result: str | None = None


def _hybrid_view(session: Session) -> HybridView:
    from app.agents.hybrid import get_or_create_hybrid_config

    cfg = get_or_create_hybrid_config(session)
    return HybridView(
        enabled=cfg.enabled, interval_seconds=cfg.interval_seconds,
        min_confidence=cfg.min_confidence, last_run_at=_iso_utc(cfg.last_run_at),
        last_result=cfg.last_result,
    )


@router.get("/hybrid", response_model=HybridView, tags=["hybrid"])
def hybrid_state(session: Session = Depends(get_session)) -> HybridView:
    """Hybrid auto-pilot config + the last tick's outcome."""
    return _hybrid_view(session)


@router.post("/hybrid/config", response_model=HybridView, tags=["hybrid"])
def hybrid_set_config(req: HybridConfigRequest, session: Session = Depends(get_session)) -> HybridView:
    from app.agents.hybrid import get_or_create_hybrid_config

    cfg = get_or_create_hybrid_config(session)
    if req.enabled is not None:
        cfg.enabled = req.enabled
    # The stored 'last check' summary quotes the threshold/interval that were in force when the
    # last scan ran. If either changes, that message is now stale (e.g. it still says "above 70%"
    # after you lower the bar to 60%), so clear it — the panel will repopulate on the next scan.
    changed_filter = False
    if req.interval_seconds is not None and req.interval_seconds != cfg.interval_seconds:
        cfg.interval_seconds = req.interval_seconds
        changed_filter = True
    if req.min_confidence is not None and req.min_confidence != cfg.min_confidence:
        cfg.min_confidence = req.min_confidence
        changed_filter = True
    if changed_filter:
        cfg.last_result = None
    session.commit()
    log.warning("hybrid config updated", extra={"enabled": cfg.enabled,
                "interval": cfg.interval_seconds, "min_confidence": cfg.min_confidence})
    return _hybrid_view(session)


@router.post("/hybrid/run", response_model=HybridView, tags=["hybrid"])
def hybrid_run_now(session: Session = Depends(get_session)) -> HybridView:
    """Run one Hybrid pass immediately (the 'Run now' button)."""
    from app.agents.hybrid import run_hybrid

    run_hybrid(session)
    return _hybrid_view(session)
