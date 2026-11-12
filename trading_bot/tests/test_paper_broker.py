import pytest

from trading_bot.brokers.base import BrokerError, Order, Side
from trading_bot.brokers.paper import PaperBroker


def make_broker(prices):
    return PaperBroker(starting_cash=10000, price_fn=lambda s: prices[s], slippage_bps=0)


def test_buy_reduces_cash_and_creates_position():
    b = make_broker({"AAA": 100.0})
    fill = b.submit_market_order(Order("AAA", Side.BUY, 10))
    assert fill.quantity == 10 and fill.price == 100.0
    acct = b.get_account()
    assert acct.cash == pytest.approx(9000)
    assert acct.equity == pytest.approx(10000)
    assert b.get_positions()["AAA"].quantity == 10


def test_buy_insufficient_cash_raises():
    b = make_broker({"AAA": 100.0})
    with pytest.raises(BrokerError):
        b.submit_market_order(Order("AAA", Side.BUY, 1000))


def test_sell_more_than_held_raises():
    b = make_broker({"AAA": 100.0})
    b.submit_market_order(Order("AAA", Side.BUY, 5))
    with pytest.raises(BrokerError):
        b.submit_market_order(Order("AAA", Side.SELL, 10))


def test_realized_pnl_on_sell():
    prices = {"AAA": 100.0}
    b = PaperBroker(10000, price_fn=lambda s: prices[s], slippage_bps=0)
    b.submit_market_order(Order("AAA", Side.BUY, 10))
    prices["AAA"] = 120.0
    fill = b.submit_market_order(Order("AAA", Side.SELL, 10))
    realized = b.realized_pnl_for_sell("AAA", fill.quantity, fill.price)
    assert realized == pytest.approx(200.0)  # (120 - 100) * 10
    assert "AAA" not in b.get_positions()


def test_average_price_on_add():
    prices = {"AAA": 100.0}
    b = PaperBroker(10000, price_fn=lambda s: prices[s], slippage_bps=0)
    b.submit_market_order(Order("AAA", Side.BUY, 10))   # @100
    prices["AAA"] = 200.0
    b.submit_market_order(Order("AAA", Side.BUY, 10))   # @200
    pos = b.get_positions()["AAA"]
    assert pos.quantity == 20
    assert pos.avg_price == pytest.approx(150.0)


def test_snapshot_restore_roundtrip():
    b = make_broker({"AAA": 100.0})
    b.submit_market_order(Order("AAA", Side.BUY, 3))
    snap = b.snapshot()
    b2 = make_broker({"AAA": 100.0})
    b2.restore(snap)
    assert b2.get_positions()["AAA"].quantity == 3
    assert b2.get_account().cash == pytest.approx(9700)
