"""Command-line interface.

    python -m trading_bot once      [-c config.yaml]   # single cycle, prints what it would/did do
    python -m trading_bot run       [-c config.yaml]   # continuous loop
    python -m trading_bot backtest  [-c config.yaml]   # rule-layer backtest
    python -m trading_bot status    [-c config.yaml]   # print account + positions
    python -m trading_bot signals   [-c config.yaml]   # print current signals only
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _confirm_live(cfg) -> bool:
    if cfg.mode != "live":
        return True
    print("\n*** LIVE MODE ***")
    print("This will place REAL orders on Robinhood via an unofficial API (against their ToS).")
    reply = input("Type 'I UNDERSTAND' to continue: ").strip()
    return reply == "I UNDERSTAND"


def cmd_status(cfg) -> int:
    from .factory import build_engine
    engine = build_engine(cfg)
    acct = engine.broker.get_account()
    print(f"mode={cfg.mode} broker={engine.broker.name}")
    print(f"cash=${acct.cash:,.2f}  equity=${acct.equity:,.2f}")
    print(f"realized P&L (all time)=${engine.state.realized_pnl:,.2f}")
    positions = engine.broker.get_positions()
    if not positions:
        print("no open positions")
    for s, p in positions.items():
        price = engine.broker.get_price(s)
        print(f"  {s:6s} qty={p.quantity:<10g} avg=${p.avg_price:,.2f} "
              f"last=${price:,.2f} uPnL=${p.unrealized_pnl(price):,.2f}")
    return 0


def cmd_signals(cfg) -> int:
    from .factory import build_engine
    engine = build_engine(cfg)
    signals = engine._compute_signals()
    for s, sig in signals.items():
        print(f"{s:6s} score={sig.score:+.2f} [{sig.candidate:4s}] {sig.detail}")
    return 0


def cmd_once(cfg, force: bool) -> int:
    from .factory import build_engine
    engine = build_engine(cfg)
    result = engine.run_cycle(force=force)
    if result.note:
        print(f"note: {result.note}")
    print(f"decisions: {len(result.decisions)}")
    for d in result.decisions:
        print(f"  {d.action:4s} {d.symbol:6s} w={d.target_weight:.2f} "
              f"conf={d.confidence:.2f} [{d.source}] {d.rationale}")
    print(f"submitted: {len(result.orders_submitted)}")
    for o in result.orders_submitted:
        print(f"  + {o['side']:4s} {o['symbol']:6s} x{o['quantity']} @ ${o['price']}")
    print(f"rejected: {len(result.orders_rejected)}")
    for o in result.orders_rejected:
        print(f"  - {o['side']:4s} {o['symbol']:6s}: {o['reason']}")
    return 0


def cmd_run(cfg) -> int:
    from .factory import build_engine
    if not _confirm_live(cfg):
        print("aborted.")
        return 1
    engine = build_engine(cfg)
    engine.run_forever()
    return 0


def cmd_backtest(cfg) -> int:
    from .backtest import run_backtest
    from .market_data import MarketData
    result = run_backtest(cfg, MarketData(history_period="2y"))
    print(result.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading_bot", description="Agentic Robinhood trading bot")
    parser.add_argument("command",
                        choices=["once", "run", "backtest", "status", "signals"])
    parser.add_argument("-c", "--config", default=None, help="path to YAML config")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="for 'once': run even when the market is closed")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    dispatch = {
        "status": lambda: cmd_status(cfg),
        "signals": lambda: cmd_signals(cfg),
        "once": lambda: cmd_once(cfg, args.force),
        "run": lambda: cmd_run(cfg),
        "backtest": lambda: cmd_backtest(cfg),
    }
    return dispatch[args.command]()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
