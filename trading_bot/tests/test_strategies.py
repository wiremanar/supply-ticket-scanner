from trading_bot.config import StrategyConfig
from trading_bot.strategies import evaluate, rsi, sma


def test_sma_basic():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 5) is None


def test_rsi_all_gains_is_high():
    closes = list(range(1, 30))  # strictly increasing
    val = rsi(closes, 14)
    assert val is not None and val > 90


def test_rsi_all_losses_is_low():
    closes = list(range(30, 1, -1))  # strictly decreasing
    val = rsi(closes, 14)
    assert val is not None and val < 10


def test_uptrend_produces_buy_candidate():
    cfg = StrategyConfig()
    # Steady uptrend: short SMA > long SMA, positive momentum.
    closes = [100 + i for i in range(80)]
    sig = evaluate("AAA", closes, cfg)
    assert sig.score > 0
    assert sig.candidate in ("buy", "hold")
    assert sig.candidate == "buy"


def test_downtrend_produces_sell_candidate():
    cfg = StrategyConfig()
    closes = [200 - i for i in range(80)]
    sig = evaluate("AAA", closes, cfg)
    assert sig.score < 0
    assert sig.candidate == "sell"


def test_score_is_clamped():
    cfg = StrategyConfig()
    closes = [100 * (1.5 ** i) for i in range(80)]  # explosive growth
    sig = evaluate("AAA", closes, cfg)
    assert -1.0 <= sig.score <= 1.0
