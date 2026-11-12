"""Wire the pieces together into a ready-to-run engine."""

from __future__ import annotations

import logging

from .config import Config
from .engine import TradingEngine
from .llm_advisor import LLMAdvisor
from .market_data import MarketData
from .portfolio import PortfolioState


def build_engine(cfg: Config) -> TradingEngine:
    market = MarketData()
    advisor = LLMAdvisor(cfg.llm)
    state = PortfolioState.load(cfg.state_path)

    if cfg.mode == "live":
        # Imported lazily so paper mode never needs robin_stocks installed.
        from .brokers.robinhood import RobinhoodBroker
        broker = RobinhoodBroker()
        logging.getLogger("trading_bot").warning(
            "LIVE mode: real orders will be placed on Robinhood via an unofficial API."
        )
    else:
        from .brokers.paper import PaperBroker
        broker = PaperBroker(cfg.paper_starting_cash, price_fn=market.get_price)

    engine = TradingEngine(cfg, broker, market, advisor, state)
    engine.restore_paper()
    return engine
