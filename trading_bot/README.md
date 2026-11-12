# Agentic Robinhood Trading Bot

A hybrid (rule-based + LLM) trading engine with a pluggable broker backend. It
scans a universe of symbols, scores each with technical indicators, lets Claude
arbitrate the candidates, passes every resulting order through a deterministic
risk manager, and executes on the chosen broker.

It runs **fully autonomously** once started (no per-trade prompt), but a bot
without limits is just a way to lose money quickly, so every order is gated by
hard, non-negotiable risk rules that the LLM cannot override.

---

## ⚠️ Read this before going live

- **Robinhood has no official retail equities API.** Live mode drives the
  private app endpoints through the unofficial [`robin_stocks`](https://github.com/jmfernandes/robin_stocks)
  library. **This violates Robinhood's Terms of Service** and can get your
  account flagged, restricted, or permanently locked. Use at your own risk.
- **Autonomous trading can lose real money fast.** The LLM is not a financial
  advisor and can be wrong; the technical signals are simplistic. Nothing here
  is investment advice.
- **Start in paper mode.** The bot defaults to `mode: paper` and will not place
  a real order until you explicitly set `mode: live` *and* provide Robinhood
  credentials via environment variables.
- You are solely responsible for any trades this software places and for
  complying with Robinhood's terms and applicable law/tax rules.

---

## Install

```bash
cd trading_bot
pip install -r requirements.txt     # core; omit robin_stocks/pyotp for paper-only
cp config.example.yaml config.yaml  # then edit
```

Paper mode and backtesting need only the core deps (`anthropic`, `pyyaml`,
`yfinance`, `pandas`). `robin_stocks` and `pyotp` are required **only** for live
mode.

## Configure

Edit `config.yaml` (see `config.example.yaml` for every field). Secrets never go
in the file — they come from the environment:

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Enables the Claude decision layer. Without it, the bot falls back to rule-based signals only. |
| `TRADING_BOT_MODEL` | Overrides the model id (default `claude-sonnet-5-5`). Set to a model your key can access. |
| `TRADING_BOT_MODE` | Overrides `mode` (`paper`/`live`). |
| `ROBINHOOD_USERNAME` / `ROBINHOOD_PASSWORD` | Live mode only. |
| `ROBINHOOD_MFA_SECRET` | Live mode only; TOTP secret for automated 2FA. |

## Run

All commands take `-c config.yaml`.

```bash
python -m trading_bot signals     # print current indicator scores, trade nothing
python -m trading_bot once        # run ONE cycle (paper), print decisions + fills
python -m trading_bot once --force  # run even when the market is closed
python -m trading_bot backtest    # walk-forward backtest of the rule layer
python -m trading_bot status      # account, positions, P&L
python -m trading_bot run         # continuous autonomous loop
```

In live mode, `run` requires typing `I UNDERSTAND` once at startup before the
loop begins.

## How a cycle works

```
history + price ─▶ rule signals (SMA / RSI / momentum) ─▶ buy/sell candidates
                                                               │
                                                               ▼
                                           Claude arbitrates ─▶ decisions
                                                               │
                                                               ▼
                               deterministic RISK MANAGER (final say) ─▶ orders
                                                               │
                                                               ▼
                                                 broker executes + audit log
```

- **Rule layer** (`strategies.py`): SMA crossover, RSI bands, and momentum are
  blended into a composite score in `[-1, 1]`. Scores past the buy/sell
  thresholds become candidates.
- **LLM layer** (`llm_advisor.py`): Claude sees only the candidate symbols plus
  the portfolio and returns a bounded action + target weight + one-line
  rationale per symbol. It can only choose among the symbols shown. If it's
  disabled, keyless, or returns junk, the bot uses the rule candidates.
- **Risk manager** (`risk.py`): deterministic and always last. Enforces
  per-position concentration, portfolio-wide exposure, a cash reserve,
  per-order notional cap, a daily-loss circuit breaker, a daily trade cap, an
  equity floor, and no shorting (configurable). It shrinks or rejects orders;
  it never enlarges them.
- **Brokers** (`brokers/`): `PaperBroker` simulates fills with slippage;
  `RobinhoodBroker` places real market orders. Same interface either way.

## Safety rails (all in `config.yaml` under `risk:`)

| Rail | Default | Meaning |
| --- | --- | --- |
| `max_position_pct` | 20% | Cap on any single position vs equity |
| `max_portfolio_invested_pct` | 95% | Cap on total invested |
| `min_cash_reserve_pct` | 5% | Cash never spent below this |
| `max_order_notional` | $2,000 | Largest single order |
| `daily_loss_limit_pct` | 3% | Halt trading for the day after this drawdown |
| `max_trades_per_day` | 20 | Daily order cap |
| `min_equity_floor` | $100 | Stop opening positions below this equity |
| `allow_shorting` | false | Only sell what you hold |

**Kill switch:** stop the process (Ctrl-C). To force a halt without stopping,
set `daily_loss_limit_pct` very low, or delete/clear `trading_bot_state.json`
to reset day state. State (positions, day counters, audit log) persists to
`state_path` between runs.

## Tests

```bash
python -m pytest trading_bot/tests/ -q
```

Covers the risk manager, paper broker accounting, and signal math. The engine
and brokers are designed so the whole pipeline runs against a synthetic price
function with no network or API key.

## Layout

```
trading_bot/
  config.py        # typed config + YAML/env loading
  market_data.py   # yfinance quotes & history (cached)
  strategies.py    # rule-based signal engine (pure, testable)
  llm_advisor.py   # Claude decision layer (+ rule fallback)
  risk.py          # deterministic risk gate
  portfolio.py     # persistent state + audit log
  engine.py        # the agentic loop
  backtest.py      # walk-forward rule-layer backtester
  factory.py       # build_engine()
  cli.py           # argparse entrypoints
  brokers/         # base / paper / robinhood
  tests/
```
