"""Agentic Robinhood trading bot.

A hybrid (rule-based + LLM) trading engine with a pluggable broker backend.
Defaults to paper trading; live Robinhood execution is opt-in and requires
credentials plus an explicit ``mode: live`` setting.
"""

__version__ = "0.1.0"
