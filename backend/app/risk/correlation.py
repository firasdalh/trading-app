"""Correlation / concentration model for the Risk Manager.

A senior trader knows that several positions can really be ONE bet: short EURUSD + short GBPUSD
+ short AUDUSD is just "long USD" three times — if the dollar turns, they all lose together.
This module maps each (symbol, direction) to its signed RISK FACTORS (each currency, plus the
crypto / equity-index / metal / energy blocs), so the Risk Manager can refuse a new trade that
would stack a 3rd correlated position onto a factor the open book is already loaded on.

Pure functions of their inputs — no DB, no network — so they're fully unit-testable.
"""
from __future__ import annotations

from collections import defaultdict

# Currencies we parse out of FX pairs (and metal/crypto quote legs).
_CCY = {
    "USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD", "CNY", "HKD", "SGD", "MXN",
    "ZAR", "TRY", "NOK", "SEK", "PLN",
}
_CRYPTO = ("BTC", "ETH", "LTC", "XRP", "BNB", "SOL", "DOGE", "ADA", "BCH", "TRX", "DOT")
_INDEX = ("US500", "US30", "USTEC", "US2000", "UK100", "DE40", "DE30", "JP225", "HK50",
          "FR40", "AUS200", "STOXX", "IN50", "NAS", "SPX", "NDX")
_ENERGY = ("USOIL", "UKOIL", "WTI", "BRENT", "OIL", "NGAS", "XNG", "XBR", "XTI")


def exposure_factors(symbol: str, direction: str) -> dict[str, int]:
    """Signed risk factors of a (symbol, direction). Long = +1 / short = -1 on each factor.

    Examples (long):
      EURUSD -> {EUR:+1, USD:-1}      USDJPY -> {USD:+1, JPY:-1}
      BTCUSD -> {CRYPTO:+1, USD:-1}   US500  -> {EQUITY:+1}
      XAUUSD -> {METAL:+1, USD:-1}    USOIL  -> {ENERGY:+1}
    """
    sign = 1 if direction == "long" else -1
    # Keep alphanumerics (index symbols like US500/JP225 carry digits); just drop separators/suffix.
    s = "".join(ch for ch in (symbol or "").upper() if ch.isalnum())
    out: dict[str, int] = {}

    if any(k in s for k in _CRYPTO):
        out["CRYPTO"] = sign
        if "USD" in s:
            out["USD"] = -sign
        return out
    if "XAU" in s or "XAG" in s or "XPT" in s or "XPD" in s:
        out["METAL"] = sign
        if "USD" in s:
            out["USD"] = -sign
        return out
    if any(k in s for k in _ENERGY):
        out["ENERGY"] = sign
        return out
    if any(k in s for k in _INDEX):
        out["EQUITY"] = sign  # risk-on equity bloc (indices move together)
        return out
    # FX pair: base + quote currencies.
    if len(s) >= 6:
        base, quote = s[:3], s[3:6]
        if base in _CCY and quote in _CCY:
            out[base] = out.get(base, 0) + sign
            out[quote] = out.get(quote, 0) - sign
            return out
    return out


def correlated_concentration(
    open_positions: list[tuple[str, str]],
    symbol: str,
    direction: str,
    limit: int = 2,
) -> str | None:
    """Reason string if opening (symbol, direction) would over-concentrate a risk factor.

    ``open_positions`` is a list of (symbol, direction) already open. We sum the net signed
    exposure per factor across them; if the new trade adds to a factor that already nets
    ``limit`` (default 2) in the SAME direction, it would be a 3rd correlated bet — block it.
    Offsetting exposure (e.g. long EURUSD then long USDJPY) is allowed: it nets DOWN, not up.
    """
    prop = exposure_factors(symbol, direction)
    if not prop:
        return None
    net: dict[str, int] = defaultdict(int)
    for sym, d in open_positions:
        for f, sgn in exposure_factors(sym, d).items():
            net[f] += sgn

    _names = {"USD": "USD", "EQUITY": "the equity-index bloc",
              "CRYPTO": "the crypto bloc", "METAL": "metals", "ENERGY": "energy"}
    for f, sgn in prop.items():
        existing = net.get(f, 0)
        if (sgn > 0 and existing >= limit) or (sgn < 0 and existing <= -limit):
            way = "long" if (existing > 0) else "short"
            label = _names.get(f, f)
            return (f"{abs(existing)} open positions already net {way} {label} — adding this would "
                    "be a 3rd correlated bet (concentration risk)")
    return None
