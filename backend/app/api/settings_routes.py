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
from app.core.state import get_or_create_daily_state, get_or_create_risk_config, get_or_create_settings
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
    loss_cooldown_minutes: int | None = None
    # Master on/off for the daily-loss circuit breaker (demo-account testing convenience).
    daily_loss_breaker_enabled: bool | None = None
    # Additional entry circuit breakers (all optional; 0 / False disables each).
    max_trades_per_day: int | None = None
    max_consecutive_losses: int | None = None
    breaker_cooldown_minutes: int | None = None
    perf_breaker_enabled: bool | None = None
    min_expectancy_r: float | None = None
    expectancy_window: int | None = None
    # Spread gate (execution-cost guard): veto entries whose live spread is too big a share of R.
    spread_gate_enabled: bool | None = None
    max_spread_r_fraction: float | None = None
    # Weekend-gap protection: block new entries before the Friday close, and optionally flatten.
    weekend_block_enabled: bool | None = None
    weekend_block_hours: float | None = None
    weekend_flatten_enabled: bool | None = None
    weekend_flatten_hours: float | None = None
    # Per-symbol scorecard: how many closed trades before judging, and warn-only vs auto-disable.
    scorecard_min_trades: int | None = None
    scorecard_auto_disable: bool | None = None


def _check_phrase(phrase: str | None) -> None:
    expected = get_settings().live_confirm_phrase
    if not phrase or phrase.strip() != expected:
        raise HTTPException(status_code=403, detail="live confirmation phrase does not match")


@router.post("/settings/mode", response_model=SettingsResponse)
def set_mode(req: ModeRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    settings = get_or_create_settings(session)

    if req.mode == ExecutionMode.C_AUTO_LIVE:
        # Mode C (auto-execute live) was removed at the user's request — the Hybrid auto-pilot is the
        # automation path (and still requires live-confirmation to touch a real account).
        raise HTTPException(
            status_code=400,
            detail="Mode C (auto-execute live) has been removed — use the Hybrid auto-pilot for "
                   "automation.",
        )
    if req.mode == ExecutionMode.B_AUTO_PAPER:
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
    if req.loss_cooldown_minutes is not None:
        if req.loss_cooldown_minutes < 0:
            raise HTTPException(status_code=400, detail="loss_cooldown_minutes must be >= 0")
        # Shortening this weakens the RISK.md "one stop becomes three" guard — log it (loud on live).
        log.warning("loss_cooldown_minutes changed", extra={"from": risk.loss_cooldown_minutes,
                                                            "to": req.loss_cooldown_minutes})
        risk.loss_cooldown_minutes = req.loss_cooldown_minutes

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

    # Additional entry breakers (0 / False disables each — these only ADD protection).
    if req.max_trades_per_day is not None:
        if req.max_trades_per_day < 0:
            raise HTTPException(status_code=400, detail="max_trades_per_day must be >= 0 (0 = off)")
        risk.max_trades_per_day = req.max_trades_per_day
    if req.max_consecutive_losses is not None:
        if req.max_consecutive_losses < 0:
            raise HTTPException(status_code=400, detail="max_consecutive_losses must be >= 0 (0 = off)")
        risk.max_consecutive_losses = req.max_consecutive_losses
    if req.breaker_cooldown_minutes is not None:
        if req.breaker_cooldown_minutes < 0:
            raise HTTPException(status_code=400, detail="breaker_cooldown_minutes must be >= 0")
        risk.breaker_cooldown_minutes = req.breaker_cooldown_minutes
    if req.perf_breaker_enabled is not None:
        risk.perf_breaker_enabled = req.perf_breaker_enabled
    if req.min_expectancy_r is not None:
        risk.min_expectancy_r = req.min_expectancy_r
    if req.expectancy_window is not None:
        if req.expectancy_window < 1:
            raise HTTPException(status_code=400, detail="expectancy_window must be >= 1")
        risk.expectancy_window = req.expectancy_window

    # Spread gate. Turning it OFF or raising the fraction lets wider execution cost through, so
    # (like the daily-loss breaker) a weakening change is logged rather than applied silently.
    if req.spread_gate_enabled is not None:
        risk.spread_gate_enabled = req.spread_gate_enabled
        if not req.spread_gate_enabled:
            log.warning("SPREAD GATE DISABLED — entries no longer checked against execution cost")
    if req.max_spread_r_fraction is not None:
        if req.max_spread_r_fraction < 0:
            raise HTTPException(status_code=400, detail="max_spread_r_fraction must be >= 0 (0 = off)")
        if req.max_spread_r_fraction > risk.max_spread_r_fraction:
            log.warning("spread gate loosened", extra={"from": risk.max_spread_r_fraction,
                                                       "to": req.max_spread_r_fraction})
        risk.max_spread_r_fraction = req.max_spread_r_fraction

    # Weekend-gap protection. Turning the BLOCK off re-exposes the book to the one risk a stop can't
    # cover (the -8.9R UKOILm gap), so it's logged like the other protections. FLATTEN closes live
    # positions, so enabling it is logged too — the user should see it in the audit trail either way.
    if req.weekend_block_enabled is not None:
        risk.weekend_block_enabled = req.weekend_block_enabled
        if not req.weekend_block_enabled:
            log.warning("WEEKEND ENTRY GUARD DISABLED — new trades may be carried through the gap")
    if req.weekend_block_hours is not None:
        if req.weekend_block_hours < 0:
            raise HTTPException(status_code=400, detail="weekend_block_hours must be >= 0 (0 = off)")
        risk.weekend_block_hours = req.weekend_block_hours
    if req.weekend_flatten_enabled is not None:
        risk.weekend_flatten_enabled = req.weekend_flatten_enabled
        log.warning("weekend flatten %s", "ENABLED" if req.weekend_flatten_enabled else "disabled")
    if req.weekend_flatten_hours is not None:
        if req.weekend_flatten_hours < 0:
            raise HTTPException(status_code=400, detail="weekend_flatten_hours must be >= 0 (0 = off)")
        risk.weekend_flatten_hours = req.weekend_flatten_hours

    # Scorecard. A LOWER min_trades makes the system quicker to condemn a symbol on a small sample,
    # which is the mistake this threshold exists to prevent — so it's floored and the change logged.
    if req.scorecard_min_trades is not None:
        if req.scorecard_min_trades < 10:
            raise HTTPException(
                status_code=400,
                detail="scorecard_min_trades must be >= 10 — judging a symbol on fewer trades than "
                       "that mistakes a normal losing streak for a broken edge")
        if req.scorecard_min_trades < risk.scorecard_min_trades:
            log.warning("scorecard threshold lowered", extra={"from": risk.scorecard_min_trades,
                                                              "to": req.scorecard_min_trades})
        risk.scorecard_min_trades = req.scorecard_min_trades
    if req.scorecard_auto_disable is not None:
        risk.scorecard_auto_disable = req.scorecard_auto_disable
        log.warning("scorecard auto-disable %s",
                    "ENABLED — symbols may be switched off automatically"
                    if req.scorecard_auto_disable else "disabled (warn only)")

    session.commit()
    log.info("risk config updated")
    return build_settings_response(session)


class WaitEntryRequest(BaseModel):
    # ATRs better than the market price to ask for. 0 = off. Capped at 0.5 deliberately: 0.75 LOSES
    # money in both halves of the backtest (~70% of trades never fill, and the survivors carry a stop
    # so tight that noise takes them out), so the UI must not let it be set there.
    atr: float = Field(0.0, ge=0.0, le=0.5)


@router.post("/settings/wait-entry", response_model=SettingsResponse)
def set_wait_entry(req: WaitEntryRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """"Wait, don't chase": turn a market entry into a LIMIT arm this many ATRs on the better side.

    The stop is structural and doesn't move, so a better fill both shrinks R and lengthens the run to
    target. Costs roughly one trade in four (price never comes back). 0 = enter at market."""
    settings = get_or_create_settings(session)
    settings.wait_entry_atr = req.atr
    session.commit()
    log.warning("wait-entry set", extra={"atr": req.atr, "mode": "limit-arm" if req.atr else "market"})
    return build_settings_response(session)


class TrendOnlyRequest(BaseModel):
    enabled: bool


@router.post("/settings/trend-only", response_model=SettingsResponse)
def set_trend_only(req: TrendOnlyRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle trend-only mode: when ON, the engine trades only a clear (ADX>=25) trend and stands
    aside in moderate/ranging/volatile regimes (best risk-adjusted result in backtests)."""
    settings = get_or_create_settings(session)
    settings.trend_only_mode = req.enabled
    session.commit()
    log.info("trend-only mode set", extra={"enabled": req.enabled})
    return build_settings_response(session)


class StBandModeRequest(BaseModel):
    enabled: bool


@router.post("/settings/st-band-mode", response_model=SettingsResponse)
def set_st_band_mode(req: StBandModeRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle the SuperTrend + EMA20-band breakout strategy: when ON, the engine trades only that
    mechanical strategy (long above the band in a SuperTrend uptrend / short below it in a downtrend;
    stop trails the SuperTrend line). Overrides the AI decider while on. Reversible."""
    settings = get_or_create_settings(session)
    settings.st_band_mode = req.enabled
    session.commit()
    log.info("st-band mode set", extra={"enabled": req.enabled})
    return build_settings_response(session)


class AiMomentumReadRequest(BaseModel):
    enabled: bool


@router.post("/settings/ai-momentum-read", response_model=SettingsResponse)
def set_ai_momentum_read(req: AiMomentumReadRequest,
                         session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle the AI momentum CLASSIFIER: when ON, at the ambiguous-momentum forks (MACD rolling over
    / RSI stretched) the AI classifies WHY momentum disagrees (healthy_pullback / weak_momentum /
    probable_reversal + evidence + confidence) and the deterministic engine decides enter/wait/reject/
    arm from it. The AI only labels — it never decides direction/levels or overrides. OFF reverts to
    the fixed 'arm the pullback and wait' rule. Reversible."""
    settings = get_or_create_settings(session)
    settings.ai_momentum_read = req.enabled
    session.commit()
    log.info("ai momentum-read set", extra={"enabled": req.enabled})
    return build_settings_response(session)


class AiRegimeReadRequest(BaseModel):
    enabled: bool


@router.post("/settings/ai-regime-read", response_model=SettingsResponse)
def set_ai_regime_read(req: AiRegimeReadRequest,
                       session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle the AI regime-texture CLASSIFIER: when ON, only at the ambiguous ('moderate' ADX) regime
    boundary the AI classifies the texture (emerging_trend / choppy_range / transition + evidence +
    confidence) and the deterministic engine promotes it to a trend / demotes it to a range / stands
    pat. The AI only labels — it never decides direction/levels or overrides; every gate still runs.
    OFF reverts to the fixed ADX-threshold regime. Reversible."""
    settings = get_or_create_settings(session)
    settings.ai_regime_read = req.enabled
    session.commit()
    log.info("ai regime-read set", extra={"enabled": req.enabled})
    return build_settings_response(session)


class AiPriceActionReadRequest(BaseModel):
    enabled: bool


@router.post("/settings/ai-priceaction-read", response_model=SettingsResponse)
def set_ai_priceaction_read(req: AiPriceActionReadRequest,
                            session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle the AI price-action CLASSIFIER: when ON, when a major opposing level sits in a trade's
    path the AI classifies how price will resolve there (likely_reject / likely_break / indecision +
    evidence + confidence) and the deterministic engine waits or takes the trade through the level. The
    AI only labels — it never decides direction/levels or overrides; every gate still runs. OFF reverts
    to the fixed 'respect the level and wait' rule. Reversible."""
    settings = get_or_create_settings(session)
    settings.ai_priceaction_read = req.enabled
    session.commit()
    log.info("ai priceaction-read set", extra={"enabled": req.enabled})
    return build_settings_response(session)


class DetFilterItem(BaseModel):
    key: str
    label: str
    desc: str


class DetFiltersView(BaseModel):
    filters: list[DetFilterItem]   # the catalog (key/label/desc)
    disabled: list[str]            # which keys are currently OFF


class DetFiltersRequest(BaseModel):
    disabled: list[str]            # the keys to turn OFF (all others active)


def _det_filters_view(session: Session) -> DetFiltersView:
    """Build the panel view. The "adx" filter is a PROXY for `trend_only_mode` (the existing, tuned
    ADX-strength gate), not a stored disable key — so it's reported OFF when trend-only is off."""
    from app.agents.orchestrator import DET_FILTERS
    s = get_or_create_settings(session)
    disabled = list(s.disabled_filters or [])
    if not s.trend_only_mode:
        disabled.append("adx")
    return DetFiltersView(filters=[DetFilterItem(**f) for f in DET_FILTERS], disabled=disabled)


@router.get("/settings/det-filters", response_model=DetFiltersView, tags=["settings"])
def get_det_filters(session: Session = Depends(get_session)) -> DetFiltersView:
    """The deterministic entry-checklist filters + which the user has turned off."""
    return _det_filters_view(session)


@router.post("/settings/det-filters", response_model=DetFiltersView, tags=["settings"])
def set_det_filters(req: DetFiltersRequest, session: Session = Depends(get_session)) -> DetFiltersView:
    """Turn deterministic entry filters on/off. Empty `disabled` = every filter active (tuned default).
    "adx" maps to Trend-only mode; the rest are stored in `disabled_filters` and applied to the
    deterministic engine (Run analysis / scan / hybrid deterministic path)."""
    from app.agents.orchestrator import DET_FILTER_KEYS
    s = get_or_create_settings(session)
    incoming = set(req.disabled)
    s.trend_only_mode = "adx" not in incoming                                   # adx OFF == trend-only OFF
    s.disabled_filters = [k for k in incoming if k in DET_FILTER_KEYS and k != "adx"]
    session.commit()
    log.info("det-filters set", extra={"disabled": s.disabled_filters, "trend_only": s.trend_only_mode})
    return _det_filters_view(session)


class AiReviewRequest(BaseModel):
    enabled: bool


@router.post("/settings/ai-review", response_model=SettingsResponse)
def set_ai_review(req: AiReviewRequest, session: Session = Depends(get_session)) -> SettingsResponse:
    """Toggle the AI confirm/veto REVIEW of the deterministic setup. OFF (default) takes the AI out of
    the trade decision — the deterministic engine + confidence gate decide, and the AI is kept only
    for the fundamental read. ON restores the legacy LLM technical + confirm/veto review. Reversible.
    (Repeatability testing showed the reasoning-model reviewer flips its verdict on ~82% of setups
    run-to-run, so it isn't a stable filter; a confidence>=70% gate matches it deterministically.)"""
    settings = get_or_create_settings(session)
    settings.ai_review_enabled = req.enabled
    session.commit()
    log.info("ai-review set", extra={"enabled": req.enabled})
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
    from app.models.db import Position
    from app.models.enums import PositionStatus
    from app.risk.service import _norm_symbol, live_broker_positions

    out = live_broker_positions(session)

    # Backfill the OPEN TIME from our own row when the broker didn't report one. This is what the
    # chart uses to mark the entry CANDLE (not just the price level), and a broker that omits it
    # would otherwise leave every marker off. Broker time wins when present — it's the real fill,
    # and it's the only source for trades opened directly in the terminal.
    missing = [p for p in out if p.opened_at is None]
    if missing:
        rows = session.scalars(
            select(Position).where(Position.status == PositionStatus.OPEN.value)
        ).all()
        by_symbol: dict[str, datetime] = {}
        for r in rows:
            if r.opened_at is None:
                continue
            key = _norm_symbol(r.symbol)
            # Several rows can share a symbol; keep the most recent open.
            if key not in by_symbol or r.opened_at > by_symbol[key]:
                by_symbol[key] = r.opened_at
        for p in missing:
            p.opened_at = by_symbol.get(_norm_symbol(p.symbol))

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
    interval_seconds: int | None = Field(None, ge=30, le=3600)
    # Time-based exit: auto-close a stagnant position held this many hours and still flat. 0 = off.
    max_hold_hours: float | None = Field(None, ge=0, le=240)


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
    interval_seconds: int
    max_hold_hours: float = 0.0
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
        enabled=cfg.enabled, auto_execute=cfg.auto_execute,
        interval_seconds=cfg.interval_seconds, max_hold_hours=cfg.max_hold_hours or 0.0,
        last_run_at=_iso_utc(cfg.last_run_at),
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
    if req.interval_seconds is not None:
        cfg.interval_seconds = req.interval_seconds
    if req.max_hold_hours is not None:
        cfg.max_hold_hours = req.max_hold_hours
    session.commit()
    log.warning("advisor config updated", extra={"enabled": cfg.enabled,
                "auto_execute": cfg.auto_execute, "interval": cfg.interval_seconds,
                "max_hold_hours": cfg.max_hold_hours})
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
    conditional_enabled: bool | None = None
    max_armed: int | None = Field(None, ge=0, le=10)


class HybridView(BaseModel):
    enabled: bool
    interval_seconds: int
    min_confidence: float
    conditional_enabled: bool
    max_armed: int
    last_run_at: str | None = None
    last_result: str | None = None


def _hybrid_view(session: Session) -> HybridView:
    from app.agents.hybrid import get_or_create_hybrid_config

    cfg = get_or_create_hybrid_config(session)
    return HybridView(
        enabled=cfg.enabled, interval_seconds=cfg.interval_seconds,
        min_confidence=cfg.min_confidence, conditional_enabled=cfg.conditional_enabled,
        max_armed=cfg.max_armed, last_run_at=_iso_utc(cfg.last_run_at),
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
    if req.conditional_enabled is not None:
        cfg.conditional_enabled = req.conditional_enabled
    if req.max_armed is not None:
        cfg.max_armed = req.max_armed
    if changed_filter:
        cfg.last_result = None
    session.commit()
    log.warning("hybrid config updated", extra={"enabled": cfg.enabled,
                "interval": cfg.interval_seconds, "min_confidence": cfg.min_confidence})
    return _hybrid_view(session)


@router.post("/hybrid/run", response_model=HybridView, tags=["hybrid"])
def hybrid_run_now(timeframe: str | None = None,
                   session: Session = Depends(get_session)) -> HybridView:
    """Run one Hybrid pass immediately (the 'Run now' button). ``timeframe`` (from the watchlist
    timeframe selector) scans + opens on that timeframe instead of each pair's own."""
    from app.agents.hybrid import run_hybrid

    run_hybrid(session, tf_override=timeframe)
    return _hybrid_view(session)


class HybridStatsView(BaseModel):
    since: str                # ISO start of the counting window (today, UTC)
    scans: int                # Hybrid ticks that actually scanned the watchlist
    scanned: int              # total pairs the AI decider evaluated across today's scans
    candidates: int           # risk-approved OPENS that cleared the confidence bar (ranking pool)
    ai_opens: int             # pairs the AI decider chose to OPEN (a market direction)
    ai_arms: int              # pairs the AI decider chose to ARM (a pending break/pullback order)
    accept_rate: float | None  # (opens + arms) / scanned — how often the AI chose to ACT, None if no scan
    direct_trades: int        # market orders the Hybrid auto-opened
    armed_setups: int         # "wait for the break" conditionals the Hybrid actually armed
    triggered_armed: int      # armed setups whose level broke and fired
    skipped_low_conf: int     # real setups skipped for being below the confidence threshold
    last_opened: str | None = None      # most recent symbol+direction the auto-pilot opened (any day)
    last_opened_at: str | None = None   # when it opened


@router.get("/hybrid/stats", response_model=HybridStatsView, tags=["hybrid"])
def hybrid_stats(session: Session = Depends(get_session)) -> HybridStatsView:
    """Today's Hybrid activity — how the auto-pilot's funnel (scan → candidates → AI review →
    open / arm → trigger) actually played out, aggregated from the per-tick run records and the
    hybrid-sourced conditionals. Resets at UTC midnight."""
    from sqlalchemy import func

    from app.models.db import AgentRun, ConditionalSetup

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    runs = session.scalars(
        select(AgentRun).where(AgentRun.agent == "hybrid", AgentRun.created_at >= day_start)
    ).all()

    scans = scanned = candidates = ai_opens = ai_arms = direct = skipped = 0
    for r in runs:
        detail = r.detail or {}
        if detail.get("opened"):
            direct += 1
        s = detail.get("stats") or {}
        if s.get("reached_scan"):
            scans += 1
        scanned += int(s.get("scanned") or 0)
        candidates += int(s.get("candidates") or 0)
        skipped += int(s.get("skipped_low_conf") or 0)
        ai_opens += int(s.get("ai_opens") or 0)
        ai_arms += int(s.get("ai_arms") or 0)

    armed = session.scalar(
        select(func.count()).select_from(ConditionalSetup).where(
            ConditionalSetup.source == "hybrid", ConditionalSetup.created_at >= day_start)
    ) or 0
    triggered = session.scalar(
        select(func.count()).select_from(ConditionalSetup).where(
            ConditionalSetup.source == "hybrid", ConditionalSetup.triggered_at >= day_start)
    ) or 0

    # "Act rate" — of the pairs the AI decider evaluated, the share it chose to act on (open or arm).
    accept_rate = round((ai_opens + ai_arms) / scanned, 2) if scanned else None

    # Most recent trade the auto-pilot opened, across all time (context — not limited to today).
    last_opened = last_opened_at = None
    for r in session.scalars(
        select(AgentRun).where(AgentRun.agent == "hybrid")
        .order_by(AgentRun.id.desc()).limit(200)
    ):
        op = (r.detail or {}).get("opened")
        if op and op.get("symbol"):
            last_opened = f"{op.get('symbol')} {op.get('direction') or ''}".strip()
            last_opened_at = _iso_utc(r.created_at)
            break

    return HybridStatsView(
        since=day_start.isoformat(), scans=scans, scanned=scanned, candidates=candidates,
        ai_opens=ai_opens, ai_arms=ai_arms, accept_rate=accept_rate, direct_trades=direct,
        armed_setups=int(armed), triggered_armed=int(triggered), skipped_low_conf=skipped,
        last_opened=last_opened, last_opened_at=last_opened_at,
    )
