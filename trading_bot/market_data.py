"""Market data access via yfinance.

Keeps a tiny in-process cache so a single trading cycle doesn't hammer the
data source for the same symbol. yfinance has no key requirement, which keeps
paper mode fully runnable out of the box.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None


class MarketDataError(RuntimeError):
    pass


@dataclass
class _CacheEntry:
    value: object
    expires_at: float


class MarketData:
    def __init__(self, history_period: str = "6mo", price_ttl: float = 30.0):
        if yf is None:
            raise MarketDataError(
                "yfinance is not installed. Run `pip install yfinance` to fetch market data."
            )
        self.history_period = history_period
        self.price_ttl = price_ttl
        self._price_cache: dict[str, _CacheEntry] = {}
        self._history_cache: dict[str, _CacheEntry] = {}

    def get_price(self, symbol: str) -> float:
        now = time.time()
        hit = self._price_cache.get(symbol)
        if hit and hit.expires_at > now:
            return hit.value  # type: ignore[return-value]

        ticker = yf.Ticker(symbol)
        price = None
        # fast_info is cheap and usually populated during/after market hours.
        try:
            price = float(ticker.fast_info["last_price"])
        except Exception:
            price = None
        if price is None or price != price:  # None or NaN
            hist = ticker.history(period="1d")
            if hist.empty:
                raise MarketDataError(f"no price available for {symbol}")
            price = float(hist["Close"].iloc[-1])

        self._price_cache[symbol] = _CacheEntry(price, now + self.price_ttl)
        return price

    def get_history(self, symbol: str):
        """Return a DataFrame of daily OHLCV bars for ``symbol``."""
        now = time.time()
        hit = self._history_cache.get(symbol)
        if hit and hit.expires_at > now:
            return hit.value

        hist = yf.Ticker(symbol).history(period=self.history_period)
        if hist.empty:
            raise MarketDataError(f"no history available for {symbol}")
        # History changes at most daily; cache for an hour.
        self._history_cache[symbol] = _CacheEntry(hist, now + 3600)
        return hist
