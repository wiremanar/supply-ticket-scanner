"""Live Robinhood broker, backed by the unofficial ``robin_stocks`` library.

IMPORTANT: Robinhood does not publish an official retail equities trading API.
``robin_stocks`` automates the private app endpoints, which is against
Robinhood's Terms of Service and can get an account flagged or locked. This
backend is opt-in and only constructed when ``mode: live``. Credentials are
read from the environment, never from the committed config.

Required environment variables:
    ROBINHOOD_USERNAME
    ROBINHOOD_PASSWORD
    ROBINHOOD_MFA_SECRET   (optional, TOTP secret for automated 2FA)
"""

from __future__ import annotations

import os

from .base import Account, Broker, BrokerError, Fill, Order, Position, Side

try:
    import robin_stocks.robinhood as rh
except ImportError:  # pragma: no cover - optional dependency
    rh = None

try:
    import pyotp
except ImportError:  # pragma: no cover
    pyotp = None


class RobinhoodBroker(Broker):
    name = "robinhood"

    def __init__(self) -> None:
        if rh is None:
            raise BrokerError(
                "robin_stocks is not installed. Run `pip install robin_stocks pyotp` for live mode."
            )
        self._logged_in = False
        self.login()

    def is_live(self) -> bool:
        return True

    def login(self) -> None:
        username = os.getenv("ROBINHOOD_USERNAME")
        password = os.getenv("ROBINHOOD_PASSWORD")
        if not username or not password:
            raise BrokerError(
                "ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD must be set for live mode."
            )
        mfa_code = None
        secret = os.getenv("ROBINHOOD_MFA_SECRET")
        if secret:
            if pyotp is None:
                raise BrokerError("pyotp is required to use ROBINHOOD_MFA_SECRET")
            mfa_code = pyotp.TOTP(secret).now()
        try:
            rh.login(username, password, mfa_code=mfa_code)
        except Exception as exc:  # pragma: no cover - network path
            raise BrokerError(f"Robinhood login failed: {exc}") from exc
        self._logged_in = True

    def get_price(self, symbol: str) -> float:
        try:
            quotes = rh.stocks.get_latest_price(symbol, includeExtendedHours=False)
            price = quotes[0] if quotes else None
            if price is None:
                raise BrokerError(f"no price for {symbol}")
            return float(price)
        except BrokerError:
            raise
        except Exception as exc:  # pragma: no cover
            raise BrokerError(f"price lookup failed for {symbol}: {exc}") from exc

    def get_positions(self) -> dict[str, Position]:
        try:
            holdings = rh.account.build_holdings()
        except Exception as exc:  # pragma: no cover
            raise BrokerError(f"failed to load holdings: {exc}") from exc
        positions: dict[str, Position] = {}
        for symbol, h in (holdings or {}).items():
            qty = float(h.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            positions[symbol] = Position(
                symbol=symbol,
                quantity=qty,
                avg_price=float(h.get("average_buy_price", 0) or 0),
            )
        return positions

    def get_account(self) -> Account:
        try:
            profile = rh.account.load_account_profile()
            cash = float(profile.get("portfolio_cash", profile.get("cash", 0)) or 0)
        except Exception as exc:  # pragma: no cover
            raise BrokerError(f"failed to load account profile: {exc}") from exc
        equity = cash
        for symbol, pos in self.get_positions().items():
            try:
                equity += pos.quantity * self.get_price(symbol)
            except Exception:
                equity += pos.quantity * pos.avg_price
        return Account(cash=cash, equity=equity)

    def submit_market_order(self, order: Order) -> Fill:
        qty = int(order.quantity)
        if qty <= 0:
            raise BrokerError("Robinhood market orders require a whole-share quantity >= 1")
        try:
            if order.side == Side.BUY:
                result = rh.orders.order_buy_market(order.symbol, qty)
            else:
                result = rh.orders.order_sell_market(order.symbol, qty)
        except Exception as exc:  # pragma: no cover
            raise BrokerError(f"order submission failed: {exc}") from exc

        if not result or result.get("detail"):
            raise BrokerError(f"order rejected by Robinhood: {result}")

        price = self.get_price(order.symbol)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            price=price,
            notional=qty * price,
            broker_order_id=result.get("id"),
        )
