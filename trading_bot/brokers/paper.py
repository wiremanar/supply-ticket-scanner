"""In-memory paper-trading broker.

Simulates immediate market fills at the current price (optionally with a small
slippage/commission model) and keeps cash + positions in memory. State is
persisted alongside the portfolio state by the engine.
"""

from __future__ import annotations

from .base import Account, Broker, BrokerError, Fill, Order, Position, Side


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, starting_cash: float, price_fn, slippage_bps: float = 1.0,
                 commission: float = 0.0):
        self._cash = float(starting_cash)
        self._positions: dict[str, Position] = {}
        self._price_fn = price_fn
        self._slippage = slippage_bps / 10_000.0
        self._commission = commission

    # --- persistence helpers -------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "cash": self._cash,
            "positions": {s: {"quantity": p.quantity, "avg_price": p.avg_price}
                          for s, p in self._positions.items()},
        }

    def restore(self, snap: dict) -> None:
        self._cash = snap.get("cash", self._cash)
        self._positions = {
            s: Position(symbol=s, quantity=d["quantity"], avg_price=d["avg_price"])
            for s, d in snap.get("positions", {}).items()
        }

    # --- Broker interface ----------------------------------------------------
    def get_price(self, symbol: str) -> float:
        return float(self._price_fn(symbol))

    def get_positions(self) -> dict[str, Position]:
        return {s: Position(p.symbol, p.quantity, p.avg_price) for s, p in self._positions.items()}

    def get_account(self) -> Account:
        equity = self._cash
        for symbol, pos in self._positions.items():
            try:
                equity += pos.quantity * self.get_price(symbol)
            except Exception:
                equity += pos.quantity * pos.avg_price
        return Account(cash=self._cash, equity=equity)

    def submit_market_order(self, order: Order) -> Fill:
        price = self.get_price(order.symbol)
        # Buyers pay up, sellers receive slightly less.
        fill_price = price * (1 + self._slippage) if order.side == Side.BUY else price * (1 - self._slippage)
        notional = order.quantity * fill_price

        if order.side == Side.BUY:
            cost = notional + self._commission
            if cost > self._cash + 1e-9:
                raise BrokerError(f"insufficient cash: need ${cost:,.2f}, have ${self._cash:,.2f}")
            self._cash -= cost
            self._apply_buy(order.symbol, order.quantity, fill_price)
        else:
            pos = self._positions.get(order.symbol)
            if not pos or pos.quantity < order.quantity - 1e-9:
                raise BrokerError(f"cannot sell {order.quantity} {order.symbol}: insufficient holdings")
            self._cash += notional - self._commission
            self._apply_sell(order.symbol, order.quantity)

        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            notional=notional,
            broker_order_id=f"paper-{order.symbol}-{order.side.value}",
        )

    def realized_pnl_for_sell(self, symbol: str, quantity: float, fill_price: float) -> float:
        """Realized P&L for a sell, using the pre-sale average cost."""
        pos = self._pre_sale_cost.get(symbol)
        if pos is None:
            return 0.0
        return (fill_price - pos) * quantity

    # --- internals -----------------------------------------------------------
    _pre_sale_cost: dict[str, float] = {}

    def _apply_buy(self, symbol: str, qty: float, price: float) -> None:
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(symbol, qty, price)
        else:
            total_qty = pos.quantity + qty
            pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total_qty
            pos.quantity = total_qty

    def _apply_sell(self, symbol: str, qty: float) -> None:
        pos = self._positions[symbol]
        self._pre_sale_cost[symbol] = pos.avg_price
        pos.quantity -= qty
        if pos.quantity <= 1e-9:
            del self._positions[symbol]
