from trading_bot.brokers.base import Account, Order, Position, Side
from trading_bot.config import RiskConfig
from trading_bot.portfolio import PortfolioState
from trading_bot.risk import RiskManager


def _state(starting_equity=10000.0):
    s = PortfolioState()
    s.start_day_if_needed(starting_equity, today="2026-01-01")
    return s


def test_buy_capped_by_position_pct():
    cfg = RiskConfig(max_position_pct=0.2, max_order_notional=None, min_cash_reserve_pct=0.0)
    rm = RiskManager(cfg)
    acct = Account(cash=10000, equity=10000)
    # Request 100 shares @ $100 = $10k, but 20% cap = $2k -> 20 shares.
    order = Order("AAA", Side.BUY, 100)
    d = rm.evaluate(order, 100.0, acct, {}, _state())
    assert d.approved
    assert d.quantity == 20


def test_buy_capped_by_order_notional():
    cfg = RiskConfig(max_order_notional=500, max_position_pct=1.0, min_cash_reserve_pct=0.0)
    rm = RiskManager(cfg)
    acct = Account(cash=10000, equity=10000)
    d = rm.evaluate(Order("AAA", Side.BUY, 100), 100.0, acct, {}, _state())
    assert d.approved and d.quantity == 5  # $500 / $100


def test_buy_respects_cash_reserve():
    cfg = RiskConfig(min_cash_reserve_pct=0.5, max_position_pct=1.0, max_order_notional=None)
    rm = RiskManager(cfg)
    acct = Account(cash=10000, equity=10000)
    # reserve = $5000; spendable $5000 -> 50 shares @ $100.
    d = rm.evaluate(Order("AAA", Side.BUY, 1000), 100.0, acct, {}, _state())
    assert d.approved and d.quantity == 50


def test_sell_clamped_to_holdings_no_shorting():
    cfg = RiskConfig(allow_shorting=False)
    rm = RiskManager(cfg)
    acct = Account(cash=0, equity=1000)
    positions = {"AAA": Position("AAA", 7, 90.0)}
    d = rm.evaluate(Order("AAA", Side.SELL, 50), 100.0, acct, positions, _state(1000))
    assert d.approved and d.quantity == 7


def test_sell_rejected_when_not_held():
    rm = RiskManager(RiskConfig(allow_shorting=False))
    acct = Account(cash=0, equity=1000)
    d = rm.evaluate(Order("AAA", Side.SELL, 5), 100.0, acct, {}, _state(1000))
    assert not d.approved


def test_daily_loss_halt():
    cfg = RiskConfig(daily_loss_limit_pct=0.03)
    rm = RiskManager(cfg)
    state = _state(10000)
    # Equity dropped to 9600 -> 4% loss > 3% limit.
    acct = Account(cash=9600, equity=9600)
    d = rm.evaluate(Order("AAA", Side.BUY, 1), 100.0, acct, {}, state)
    assert not d.approved
    assert state.day.halted


def test_max_trades_per_day():
    cfg = RiskConfig(max_trades_per_day=2, min_cash_reserve_pct=0.0, max_position_pct=1.0,
                     max_order_notional=None)
    rm = RiskManager(cfg)
    state = _state(10000)
    state.day.trades_today = 2
    acct = Account(cash=10000, equity=10000)
    d = rm.evaluate(Order("AAA", Side.BUY, 1), 100.0, acct, {}, state)
    assert not d.approved and "max_trades_per_day" in d.reason


def test_equity_floor_blocks_buy():
    rm = RiskManager(RiskConfig(min_equity_floor=1000))
    acct = Account(cash=500, equity=500)
    d = rm.evaluate(Order("AAA", Side.BUY, 1), 10.0, acct, {}, _state(500))
    assert not d.approved
