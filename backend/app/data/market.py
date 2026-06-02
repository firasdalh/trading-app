"""Market-data provider interface + a synthetic offline source.

Providers are swappable like brokers. The synthetic provider generates deterministic
random-walk OHLCV so the whole pipeline (technical agent, sim broker, backtest) runs with
no external account or network. Real providers (Alpaca, later OANDA/ccxt) implement the
same interface.
"""
from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from app.models.schemas import Candle, OHLCVSeries, Quote

# Minutes per timeframe string. Extend as needed.
_TIMEFRAME_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


def timeframe_to_minutes(timeframe: str) -> int:
    if timeframe not in _TIMEFRAME_MINUTES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return _TIMEFRAME_MINUTES[timeframe]


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries: ...


class SyntheticDataProvider(MarketDataProvider):
    """Deterministic random-walk OHLCV — seeded per symbol so results are reproducible.

    Not real data. Used for offline dev, the sim broker, and tests. The UI must label any
    output derived from this as synthetic.
    """

    name = "synthetic"

    def __init__(self, base_price: float = 100.0, volatility: float = 0.01) -> None:
        self.base_price = base_price
        self.volatility = volatility

    def _seed(self, symbol: str) -> int:
        return int(hashlib.sha256(symbol.encode()).hexdigest(), 16) % (2**32)

    def _price_at(self, symbol: str, step: int) -> float:
        """Smooth, deterministic pseudo-price as a function of step index."""
        seed = self._seed(symbol)
        # Combine a couple of sinusoids + a seeded drift; bounded and positive.
        drift = ((seed % 7) - 3) * 0.0005
        wave = (
            math.sin((step + seed % 50) / 12.0) * 0.6
            + math.sin((step + seed % 23) / 5.0) * 0.4
        )
        price = self.base_price * (1 + self.volatility * wave + drift * step / 50.0)
        return max(0.01, round(price, 4))

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        minutes = timeframe_to_minutes(timeframe)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles: list[Candle] = []
        for i in range(limit):
            step = i
            ts = now - timedelta(minutes=minutes * (limit - 1 - i))
            o = self._price_at(symbol, step)
            c = self._price_at(symbol, step + 1)
            hi = max(o, c) * (1 + self.volatility * 0.3)
            lo = min(o, c) * (1 - self.volatility * 0.3)
            vol = 1000 + (self._seed(symbol) + step) % 500
            candles.append(
                Candle(ts=ts, open=o, high=round(hi, 4), low=round(lo, 4), close=c, volume=float(vol))
            )
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=candles)

    def get_quote(self, symbol: str) -> Quote:
        series = self.get_ohlcv(symbol, "1m", limit=2)
        last = series.candles[-1]
        return Quote(symbol=symbol, price=last.close, ts=last.ts)


class AlpacaDataProvider(MarketDataProvider):
    """Real market data via alpaca-py. Lazy import so the app boots without the package."""

    name = "alpaca"

    # Map our timeframe strings to alpaca TimeFrame objects lazily.
    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("Alpaca data provider requires API key + secret")
        self._api_key = api_key
        self._api_secret = api_secret
        self.__stock_client = None
        self.__crypto_client = None

    def _stock_client(self):
        if self.__stock_client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self.__stock_client = StockHistoricalDataClient(self._api_key, self._api_secret)
        return self.__stock_client

    def _crypto_client(self):
        if self.__crypto_client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient

            self.__crypto_client = CryptoHistoricalDataClient(self._api_key, self._api_secret)
        return self.__crypto_client

    @staticmethod
    def _is_crypto(symbol: str) -> bool:
        return "/" in symbol  # alpaca crypto symbols look like "BTC/USD"

    def _alpaca_timeframe(self, timeframe: str):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        mapping = {
            "1m": TimeFrame(1, TimeFrameUnit.Minute),
            "5m": TimeFrame(5, TimeFrameUnit.Minute),
            "15m": TimeFrame(15, TimeFrameUnit.Minute),
            "30m": TimeFrame(30, TimeFrameUnit.Minute),
            "1h": TimeFrame(1, TimeFrameUnit.Hour),
            "4h": TimeFrame(4, TimeFrameUnit.Hour),
            "1d": TimeFrame(1, TimeFrameUnit.Day),
        }
        if timeframe not in mapping:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        return mapping[timeframe]

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> OHLCVSeries:
        from datetime import timedelta

        from alpaca.data.requests import (
            CryptoBarsRequest,
            StockBarsRequest,
        )

        tf = self._alpaca_timeframe(timeframe)
        minutes = timeframe_to_minutes(timeframe)
        # Pad lookback generously so we get at least `limit` bars.
        start = datetime.now(timezone.utc) - timedelta(minutes=minutes * limit * 3 + 1440)

        if self._is_crypto(symbol):
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, limit=limit)
            bars = self._crypto_client().get_crypto_bars(req)
        else:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, limit=limit)
            bars = self._stock_client().get_stock_bars(req)

        rows = bars.data.get(symbol, [])
        candles = [
            Candle(
                ts=b.timestamp,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume or 0),
            )
            for b in rows
        ]
        return OHLCVSeries(symbol=symbol, timeframe=timeframe, candles=candles[-limit:])

    def get_quote(self, symbol: str) -> Quote:
        from alpaca.data.requests import (
            CryptoLatestQuoteRequest,
            StockLatestQuoteRequest,
        )

        if self._is_crypto(symbol):
            req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            q = self._crypto_client().get_crypto_latest_quote(req)[symbol]
        else:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            q = self._stock_client().get_stock_latest_quote(req)[symbol]
        # Mid price from bid/ask; fall back to ask or bid.
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        price = (bid + ask) / 2 if bid and ask else (ask or bid)
        return Quote(symbol=symbol, price=price, ts=q.timestamp)
