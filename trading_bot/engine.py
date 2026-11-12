"""Trading engine — the agentic loop.

One cycle:
  1. Pull history + price for each symbol in the universe.
  2. Compute rule-based signals (candidates).
  3. Ask the LLM layer to arbitrate -> decisions.
  4. Turn each decision into a target order.
  5. Pass every order through the deterministic risk manager.
  6. Execute approved orders on the broker and record them.

The loop is intentionally boring and heavily logged: in autonomous live mode
the audit trail is the only way to see why the bot did what it did.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from .brokers.base import Broker, BrokerError, Order, Side
from .brokers.paper import PaperBroker
from .config import Config
from .llm_advisor import Decision, LLMAdvisor
from .market_data import MarketData
from .portfolio import PortfolioState
from .risk import RiskManager
from .strategies import Signal, evaluate_from_history

log = logging.getLogger("trading_bot")


def _is_market_open(now: datetime | None = None) -> bool:
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:  # pragma: no cover
        return True  # if tz data is unavailable, don't block
    now = (now or datetime.now(et)).astimezone(et)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


@dataclass
class CycleResult:
    timestamp: str
    signals: dict[str, Signal]
    decisions: list[Decision]
    orders_submitted: list[dict]
    orders_rejected: list[dict]
    halted: bool
    note: str = ""


class TradingEngine:
    def __init__(self, cfg: Config, broker: Broker, market: MarketData,
                 advisor: LLMAdvisor, state: PortfolioState):
        self.cfg = cfg
        self.broker = broker
        self.market = market
        self.advisor = advisor
        self.state = state
        self.risk = RiskManager(cfg.risk)

    # --- persistence ---------------------------------------------------------
    def save(self) -> None:
        if isinstance(self.broker, PaperBroker):
            self.state.log("paper_snapshot", self.broker.snapshot())
            # Store the latest snapshot in a stable key for restore.
            self.state.audit_log = [e for e in self.state.audit_log
                                    if e.get("event") != "paper_state"]
            self.state.audit_log.append({"event": "paper_state", **self.broker.snapshot()})
        self.state.save(self.cfg.state_path)

    def restore_paper(self) -> None:
        if not isinstance(self.broker, PaperBroker):
            return
        for entry in reversed(self.state.audit_log):
            if entry.get("event") == "paper_state":
                self.broker.restore({"cash": entry.get("cash"), "positions": entry.get("positions", {})})
                break

    # --- one cycle -----------------------------------------------------------
    def run_cycle(self, force: bool = False) -> CycleResult:
        ts = datetime.utcnow().isoformat()
        if self.cfg.trade_during_market_hours_only and not force and not _is_market_open():
            return CycleResult(ts, {}, [], [], [], halted=False, note="market closed")

        account = self.broker.get_account()
        positions = self.broker.get_positions()
        self.state.start_day_if_needed(account.equity)

        halt = self.risk.check_daily_halt(self.state, account.equity)
        if halt:
            self.state.log("halt", {"reason": halt, "equity": account.equity})
            self.save()
            return CycleResult(ts, {}, [], [], [], halted=True, note=halt)

        signals = self._compute_signals()
        decisions = self.advisor.decide(signals, positions, account)
        self.state.log("decisions", {
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "items": [vars(d) for d in decisions],
        })

        submitted, rejected = [], []
        for decision in decisions:
            order = self._decision_to_order(decision, positions, account)
            if order is None:
                continue
            price = self.broker.get_price(order.symbol)
            rd = self.risk.evaluate(order, price, account, positions, self.state)
            if not rd.approved:
                rejected.append({"symbol": order.symbol, "side": order.side.value,
                                 "reason": rd.reason, "rationale": decision.rationale})
                self.state.log("order_rejected", rejected[-1])
                continue
            order.quantity = rd.quantity
            try:
                fill = self.broker.submit_market_order(order)
            except BrokerError as exc:
                rejected.append({"symbol": order.symbol, "side": order.side.value,
                                 "reason": f"broker error: {exc}"})
                self.state.log("order_error", rejected[-1])
                continue

            realized = 0.0
            if order.side == Side.SELL and isinstance(self.broker, PaperBroker):
                realized = self.broker.realized_pnl_for_sell(order.symbol, fill.quantity, fill.price)
            self.state.record_fill(fill, realized)
            # Refresh local view so later decisions in the same cycle see it.
            positions = self.broker.get_positions()
            account = self.broker.get_account()
            submitted.append({"symbol": fill.symbol, "side": fill.side.value,
                              "quantity": fill.quantity, "price": round(fill.price, 4),
                              "notional": round(fill.notional, 2), "rationale": decision.rationale})
            log.info("FILLED %s %s x%s @ %.2f (%s)", fill.side.value, fill.symbol,
                     fill.quantity, fill.price, decision.source)

        self.save()
        return CycleResult(ts, signals, decisions, submitted, rejected, halted=False)

    # --- helpers -------------------------------------------------------------
    def _compute_signals(self) -> dict[str, Signal]:
        signals: dict[str, Signal] = {}
        for symbol in self.cfg.universe:
            try:
                hist = self.market.get_history(symbol)
                signals[symbol] = evaluate_from_history(symbol, hist, self.cfg.strategy)
            except Exception as exc:
                log.warning("signal failed for %s: %s", symbol, exc)
        return signals

    def _decision_to_order(self, decision: Decision, positions, account) -> Order | None:
        if decision.action == "hold":
            return None
        if decision.action == "sell":
            held = positions.get(decision.symbol)
            if not held or held.quantity <= 0:
                return None
            return Order(decision.symbol, Side.SELL, held.quantity, reason=decision.rationale)

        # buy: move the position toward target_weight of equity.
        price = self.broker.get_price(decision.symbol)
        target_value = decision.target_weight * account.equity
        held = positions.get(decision.symbol)
        current_value = (held.quantity * price) if held else 0.0
        delta_value = target_value - current_value
        if delta_value <= 0:
            return None
        qty = delta_value / price
        if qty < 1:
            return None
        return Order(decision.symbol, Side.BUY, qty, reason=decision.rationale)

    # --- long-running loop ---------------------------------------------------
    def run_forever(self) -> None:
        log.info("starting %s loop over %s (interval %ss)", self.broker.name,
                 ",".join(self.cfg.universe), self.cfg.poll_interval_sec)
        while True:
            try:
                result = self.run_cycle()
                if result.note:
                    log.info("cycle: %s", result.note)
                else:
                    log.info("cycle: %d submitted, %d rejected",
                             len(result.orders_submitted), len(result.orders_rejected))
            except KeyboardInterrupt:  # pragma: no cover
                log.info("interrupted; shutting down")
                break
            except Exception as exc:  # keep the loop alive
                log.exception("cycle failed: %s", exc)
            time.sleep(self.cfg.poll_interval_sec)
