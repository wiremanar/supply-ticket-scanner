"""Risk manager — the hard gate every order passes through.

This runs *after* the LLM has made its call. The LLM can suggest anything; the
risk manager is deterministic, auditable, and has final say. In autonomous
mode this is the only thing standing between the model and the account, so it
errs toward refusing or shrinking orders rather than trusting the suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass

from .brokers.base import Account, Order, Position, Side
from .config import RiskConfig
from .portfolio import PortfolioState


@dataclass
class RiskDecision:
    approved: bool
    quantity: float          # possibly reduced from the request
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check_daily_halt(self, state: PortfolioState, equity: float) -> str | None:
        """Return a halt reason if the daily loss limit is breached, else None."""
        day = state.day
        if day is None:
            return None
        if day.halted:
            return day.halt_reason or "trading halted for the day"
        loss = day.starting_equity - equity
        limit = self.cfg.daily_loss_limit_pct * day.starting_equity
        if limit > 0 and loss >= limit:
            reason = (
                f"daily loss limit hit: down ${loss:,.2f} "
                f"({loss / max(day.starting_equity, 1e-9):.1%}) >= limit ${limit:,.2f}"
            )
            day.halted = True
            day.halt_reason = reason
            return reason
        return None

    def evaluate(
        self,
        order: Order,
        price: float,
        account: Account,
        positions: dict[str, Position],
        state: PortfolioState,
    ) -> RiskDecision:
        cfg = self.cfg
        equity = account.equity
        qty = order.quantity

        if qty <= 0 or price <= 0:
            return RiskDecision(False, 0, "non-positive quantity or price")

        # Global circuit breakers.
        halt = self.check_daily_halt(state, equity)
        if halt:
            return RiskDecision(False, 0, halt)

        if state.day and state.day.trades_today >= cfg.max_trades_per_day:
            return RiskDecision(False, 0, f"max_trades_per_day ({cfg.max_trades_per_day}) reached")

        held = positions.get(order.symbol)

        if order.side == Side.SELL:
            return self._evaluate_sell(order, qty, held, cfg)
        return self._evaluate_buy(order, qty, price, account, positions, held, cfg)

    def _evaluate_sell(self, order, qty, held, cfg) -> RiskDecision:
        held_qty = held.quantity if held else 0.0
        if held_qty <= 0 and not cfg.allow_shorting:
            return RiskDecision(False, 0, f"no {order.symbol} position to sell (shorting disabled)")
        if not cfg.allow_shorting:
            qty = min(qty, held_qty)
        if qty <= 0:
            return RiskDecision(False, 0, "nothing to sell after clamping to holdings")
        return RiskDecision(True, qty, "sell approved")

    def _evaluate_buy(self, order, qty, price, account, positions, held, cfg) -> RiskDecision:
        equity = account.equity
        if equity < cfg.min_equity_floor:
            return RiskDecision(False, 0, f"equity ${equity:,.2f} below floor ${cfg.min_equity_floor:,.2f}")

        notional = qty * price

        # 1. Per-order notional cap.
        if cfg.max_order_notional is not None and notional > cfg.max_order_notional:
            qty = cfg.max_order_notional / price
            notional = qty * price

        # 2. Per-position concentration cap.
        held_value = (held.quantity * price) if held else 0.0
        max_position_value = cfg.max_position_pct * equity
        room = max_position_value - held_value
        if room <= 0:
            return RiskDecision(False, 0, f"{order.symbol} already at/above max_position_pct")
        if notional > room:
            qty = room / price
            notional = qty * price

        # 3. Portfolio-wide invested cap.
        invested = sum(p.quantity * price if p.symbol == order.symbol else p.quantity * p.avg_price
                       for p in positions.values())
        max_invested = cfg.max_portfolio_invested_pct * equity
        invest_room = max_invested - invested
        if invest_room <= 0:
            return RiskDecision(False, 0, "portfolio already at max_portfolio_invested_pct")
        if notional > invest_room:
            qty = invest_room / price
            notional = qty * price

        # 4. Cash reserve: never spend below the reserve floor.
        reserve = cfg.min_cash_reserve_pct * equity
        spendable = account.cash - reserve
        if spendable <= 0:
            return RiskDecision(False, 0, "no spendable cash above reserve")
        if notional > spendable:
            qty = spendable / price
            notional = qty * price

        # Round down to whole shares; the bot does not place fractional orders.
        qty = float(int(qty))
        if qty < 1:
            return RiskDecision(False, 0, "approved size rounds to less than one share")

        return RiskDecision(True, qty, f"buy approved for {int(qty)} share(s), ~${qty*price:,.2f}")
