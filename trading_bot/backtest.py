"""A simple walk-forward backtester for the rule-based layer.

This does not call the LLM — it evaluates the deterministic signal engine and
risk sizing against historical daily bars so you can sanity-check a universe
and parameter set before risking anything. It is intentionally simplified
(daily close fills, no intraday, no borrowing) and is a sanity tool, not a
precise P&L simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .strategies import evaluate


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    return_pct: float
    max_drawdown_pct: float
    num_trades: int
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"start=${self.starting_equity:,.2f} end=${self.ending_equity:,.2f} "
            f"return={self.return_pct:+.2%} maxDD={self.max_drawdown_pct:.2%} "
            f"trades={self.num_trades}"
        )


def run_backtest(cfg: Config, market, lookback_warmup: int | None = None) -> BacktestResult:
    """Replay history day by day using an equal-weight candidate policy."""
    strat = cfg.strategy
    warmup = lookback_warmup or max(strat.sma_long, strat.rsi_period, strat.momentum_period) + 1

    # Gather aligned close series per symbol.
    closes: dict[str, list[float]] = {}
    for symbol in cfg.universe:
        hist = market.get_history(symbol)
        closes[symbol] = [float(x) for x in hist["Close"].tolist()]
    n = min((len(v) for v in closes.values()), default=0)
    if n <= warmup:
        raise ValueError("not enough history to backtest")

    cash = cfg.paper_starting_cash
    shares: dict[str, float] = {s: 0.0 for s in cfg.universe}
    equity_curve: list[float] = []
    peak = cash
    max_dd = 0.0
    trades = 0
    max_names = max(1, int(1 / max(cfg.risk.max_position_pct, 1e-9)))

    for t in range(warmup, n):
        prices = {s: closes[s][t] for s in cfg.universe}
        # Signals using data up to and including day t.
        sigs = {s: evaluate(s, closes[s][: t + 1], strat) for s in cfg.universe}

        # Sells first (free up cash).
        for s in cfg.universe:
            if shares[s] > 0 and sigs[s].candidate == "sell":
                cash += shares[s] * prices[s]
                shares[s] = 0.0
                trades += 1

        # Buys: equal-weight the current buy candidates, capped by concentration.
        buys = [s for s in cfg.universe if sigs[s].candidate == "buy" and shares[s] == 0]
        equity = cash + sum(shares[s] * prices[s] for s in cfg.universe)
        if buys:
            per_name = min(cfg.risk.max_position_pct, 1.0 / max(len(buys), max_names)) * equity
            for s in buys:
                spend = min(per_name, cash)
                qty = int(spend / prices[s]) if prices[s] > 0 else 0
                if qty >= 1:
                    cash -= qty * prices[s]
                    shares[s] += qty
                    trades += 1

        equity = cash + sum(shares[s] * prices[s] for s in cfg.universe)
        equity_curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)

    ending = equity_curve[-1] if equity_curve else cfg.paper_starting_cash
    return BacktestResult(
        starting_equity=cfg.paper_starting_cash,
        ending_equity=ending,
        return_pct=(ending - cfg.paper_starting_cash) / cfg.paper_starting_cash,
        max_drawdown_pct=max_dd,
        num_trades=trades,
        equity_curve=equity_curve,
    )
