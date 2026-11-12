"""Rule-based signal layer.

Each indicator produces a sub-score in [-1, 1]; a weighted blend yields a
composite score per symbol. Symbols whose score clears the configured
thresholds become buy/sell *candidates* that the LLM layer then arbitrates.

The functions here are deliberately pure and dependency-light (they take a
pandas DataFrame of OHLCV bars) so they are easy to unit test and backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import StrategyConfig


@dataclass
class Signal:
    symbol: str
    score: float  # composite, clamped to [-1, 1]
    sma_score: float
    rsi_score: float
    momentum_score: float
    rsi_value: float
    last_price: float
    candidate: str  # "buy" | "sell" | "hold"
    detail: str = ""


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sma(values, window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(closes, period: int) -> float | None:
    """Classic Wilder-style RSI over the last ``period`` deltas."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sma_score(closes, cfg: StrategyConfig) -> float:
    short = sma(closes, cfg.sma_short)
    long = sma(closes, cfg.sma_long)
    if short is None or long is None or long == 0:
        return 0.0
    # Percentage gap between the averages, scaled so ~5% gap saturates.
    gap = (short - long) / long
    return _clamp(gap / 0.05)


def _rsi_score(rsi_value: float | None, cfg: StrategyConfig) -> float:
    if rsi_value is None:
        return 0.0
    # Oversold -> bullish (+), overbought -> bearish (-).
    if rsi_value <= cfg.rsi_oversold:
        return _clamp((cfg.rsi_oversold - rsi_value) / cfg.rsi_oversold + 0.5)
    if rsi_value >= cfg.rsi_overbought:
        return -_clamp((rsi_value - cfg.rsi_overbought) / (100 - cfg.rsi_overbought) + 0.5)
    # Neutral band: mild lean toward the midpoint.
    mid = (cfg.rsi_oversold + cfg.rsi_overbought) / 2
    return _clamp((mid - rsi_value) / (cfg.rsi_overbought - cfg.rsi_oversold))


def _momentum_score(closes, cfg: StrategyConfig) -> float:
    if len(closes) < cfg.momentum_period + 1:
        return 0.0
    past = closes[-cfg.momentum_period - 1]
    if past == 0:
        return 0.0
    ret = (closes[-1] - past) / past
    # ~10% move over the window saturates the score.
    return _clamp(ret / 0.10)


def evaluate(symbol: str, closes: list[float], cfg: StrategyConfig) -> Signal:
    """Compute the composite signal for one symbol from a close-price series."""
    rsi_value = rsi(closes, cfg.rsi_period)
    sma_s = _sma_score(closes, cfg)
    rsi_s = _rsi_score(rsi_value, cfg)
    mom_s = _momentum_score(closes, cfg)

    score = _clamp(
        cfg.weight_sma * sma_s + cfg.weight_rsi * rsi_s + cfg.weight_momentum * mom_s
    )

    if score >= cfg.buy_threshold:
        candidate = "buy"
    elif score <= cfg.sell_threshold:
        candidate = "sell"
    else:
        candidate = "hold"

    detail = (
        f"sma={sma_s:+.2f} rsi={rsi_s:+.2f}(rsi={rsi_value if rsi_value is None else round(rsi_value,1)}) "
        f"mom={mom_s:+.2f} -> {score:+.2f}"
    )
    return Signal(
        symbol=symbol,
        score=score,
        sma_score=sma_s,
        rsi_score=rsi_s,
        momentum_score=mom_s,
        rsi_value=rsi_value if rsi_value is not None else float("nan"),
        last_price=closes[-1] if closes else float("nan"),
        candidate=candidate,
        detail=detail,
    )


def evaluate_from_history(symbol: str, history, cfg: StrategyConfig) -> Signal:
    """Convenience wrapper that pulls the close column out of a DataFrame."""
    closes = [float(x) for x in history["Close"].tolist()]
    return evaluate(symbol, closes, cfg)
