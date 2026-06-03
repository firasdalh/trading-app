"""Application configuration.

All configuration is loaded from environment variables / a gitignored `.env` file.
Secrets (API keys, the live-confirm phrase) live ONLY here — never hard-coded elsewhere,
never logged, never placed in URLs.

The *risk* defaults intentionally mirror RISK.md. Those values are hard ceilings; see
``app.risk`` (Milestone 3) for the deterministic enforcement. Do not raise these here.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BrokerEnv = Literal["paper", "live"]


class Settings(BaseSettings):
    """Process-level settings, sourced from the environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    app_env: str = "local"
    log_level: str = "INFO"

    # ---- Database ----
    database_url: str = "sqlite:///./trading.sqlite3"

    # ---- Safety: env-level kill switch + broker environment ----
    kill_switch: bool = False
    broker_env: BrokerEnv = "paper"

    # ---- LLM provider selection ----
    # "anthropic" (Claude) or "gemini" (Google). UI can override per the DB LlmConfig.
    llm_provider: str = "anthropic"

    # ---- Anthropic ----
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    # ---- Google Gemini ----
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # ---- Alpaca (paper) ----
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"

    # ---- OANDA ----
    oanda_api_key: str = ""
    oanda_account_id: str = ""
    oanda_env: str = "practice"

    # ---- ccxt ----
    ccxt_exchange: str = "binance"
    ccxt_api_key: str = ""
    ccxt_api_secret: str = ""

    # ---- MetaTrader 5 (Exness and other MT5 brokers) ----
    # Requires a locally-installed MT5 terminal logged into the account. Windows only.
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""        # e.g. "Exness-MT5Trial" (demo) or your Exness real server
    mt5_path: str = ""          # optional full path to terminal64.exe

    # ---- Live confirmation ----
    live_confirm_phrase: str = "I understand this trades real money"

    # ---- Risk defaults (mirror RISK.md — hard ceilings, do not raise) ----
    # These seed the DB RiskConfig row on first boot. See RISK.md for rationale.
    default_risk_per_trade: float = Field(0.01, description="1% of equity per trade")
    risk_per_trade_ceiling: float = Field(0.02, description="Hard ceiling — never exceed 2%")
    default_max_open_positions: int = 3
    default_max_daily_loss: float = Field(0.03, description="3% of equity")
    default_max_total_exposure: float = Field(0.06, description="6% of equity at risk")
    default_per_pair_cooldown_minutes: int = 30

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def public_dict(self) -> dict:
        """A safe-to-log/serve view of settings with all secrets redacted."""
        secret_fields = {
            "anthropic_api_key",
            "alpaca_api_key",
            "alpaca_api_secret",
            "oanda_api_key",
            "oanda_account_id",
            "ccxt_api_key",
            "ccxt_api_secret",
            "mt5_password",
            "gemini_api_key",
            "live_confirm_phrase",
            "database_url",
        }
        out = {}
        for name in self.model_fields:
            value = getattr(self, name)
            if name in secret_fields:
                out[name] = _redact(value)
            else:
                out[name] = value
        return out


def _redact(value: object) -> str:
    """Show only whether a secret is set, never its content."""
    s = str(value or "")
    return "***set***" if s else "***unset***"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere instead of constructing Settings()."""
    return Settings()
