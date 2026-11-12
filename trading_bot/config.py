"""Configuration loading and validation.

Config comes from a YAML file, with a handful of secret / deployment values
pulled from environment variables so they never land in the committed file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a declared dependency
    yaml = None


@dataclass
class RiskConfig:
    """Hard guardrails applied to every order, in every mode."""

    # Max fraction of total equity a single position may represent.
    max_position_pct: float = 0.20
    # Max fraction of equity that may be invested across all positions.
    max_portfolio_invested_pct: float = 0.95
    # Cash that must always remain uninvested, as a fraction of equity.
    min_cash_reserve_pct: float = 0.05
    # Largest single order, in dollars. None disables the cap.
    max_order_notional: float | None = 2000.0
    # If the day's loss (realized + unrealized) exceeds this fraction of the
    # day's starting equity, the bot halts trading until the next session.
    daily_loss_limit_pct: float = 0.03
    # Max number of orders the bot will place per calendar day.
    max_trades_per_day: int = 20
    # Refuse to open new positions once equity falls below this dollar floor.
    min_equity_floor: float = 100.0
    # Allow short selling. Off by default — the bot only sells what it holds.
    allow_shorting: bool = False


@dataclass
class StrategyConfig:
    """Weights and thresholds for the rule-based signal layer."""

    # Lookback windows (in daily bars).
    sma_short: int = 20
    sma_long: int = 50
    rsi_period: int = 14
    momentum_period: int = 20

    # RSI bands.
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Blend weights for the composite score. Normalised at load time.
    weight_sma: float = 0.4
    weight_rsi: float = 0.3
    weight_momentum: float = 0.3

    # Composite score thresholds that make a symbol a buy/sell candidate.
    buy_threshold: float = 0.3
    sell_threshold: float = -0.3


@dataclass
class LLMConfig:
    """Settings for the Claude decision layer."""

    enabled: bool = True
    model: str = field(default_factory=lambda: os.getenv("TRADING_BOT_MODEL", "claude-sonnet-5-5"))
    max_tokens: int = 1500
    temperature: float = 0.2
    # If the LLM errors or returns junk, fall back to the rule-based candidates.
    fallback_to_rules: bool = True


@dataclass
class Config:
    # paper | live
    mode: str = "paper"
    # Symbols the bot is allowed to consider.
    universe: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "SPY"])
    # Seconds between trading cycles when running the live loop.
    poll_interval_sec: int = 300
    # Starting cash for paper mode.
    paper_starting_cash: float = 10000.0
    # Only act during US regular market hours (09:30–16:00 ET, Mon–Fri).
    trade_during_market_hours_only: bool = True
    # Where portfolio/audit state is persisted.
    state_path: str = "trading_bot_state.json"

    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    def __post_init__(self) -> None:
        self.mode = self.mode.lower().strip()
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        self.universe = [s.upper().strip() for s in self.universe if s.strip()]
        if not self.universe:
            raise ValueError("universe must contain at least one symbol")
        self._normalise_weights()

    def _normalise_weights(self) -> None:
        s = self.strategy
        total = s.weight_sma + s.weight_rsi + s.weight_momentum
        if total <= 0:
            raise ValueError("strategy weights must sum to a positive number")
        s.weight_sma /= total
        s.weight_rsi /= total
        s.weight_momentum /= total


def _build(cls, data: dict[str, Any]):
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | None = None) -> Config:
    """Load config from ``path`` (YAML). Falls back to defaults if path is None."""
    data: dict[str, Any] = {}
    if path:
        if yaml is None:
            raise RuntimeError("pyyaml is required to load a config file")
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    risk = _build(RiskConfig, data.pop("risk", {}) or {})
    strategy = _build(StrategyConfig, data.pop("strategy", {}) or {})
    llm = _build(LLMConfig, data.pop("llm", {}) or {})

    cfg = _build(Config, {**data, "risk": risk, "strategy": strategy, "llm": llm})

    # Environment overrides for operational knobs.
    if os.getenv("TRADING_BOT_MODE"):
        cfg.mode = os.environ["TRADING_BOT_MODE"].lower().strip()
        cfg.__post_init__()
    return cfg
