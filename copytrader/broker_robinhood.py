"""Robinhood adapter, built on the unofficial robin_stocks library.

WARNING: Robinhood has no official API for stocks or options. This uses the private endpoints the
Robinhood app uses. That is against Robinhood's terms of service, can get the account restricted,
and can stop working whenever Robinhood changes its app. Robinhood also has no paper trading, so
every order here is real money. Test everything in dry-run mode first.
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from .broker import Broker, OrderResult
from .parser import OPTION
from .ticks import is_occ_symbol, parse_occ, round_to_tick

log = logging.getLogger(__name__)

# Robinhood may list an index's PM-settled weeklies under the index symbol itself, so try both.
_ROOT_ALIASES = {"SPXW": "SPX", "NDXP": "NDX", "RUTW": "RUT", "VIXW": "VIX"}
_RELOGIN_SECONDS = 12 * 3600


class RobinhoodBroker(Broker):
    def __init__(
        self,
        username: str,
        password: str,
        mfa_secret: str = "",
        account_number: str = "",
        api=None,
    ):
        if not username or not password:
            raise ValueError("ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD must be set")
        if api is None:
            try:
                import robin_stocks.robinhood as api
            except ImportError as exc:
                raise SystemExit(
                    "Robinhood support needs extra packages: pip install -r requirements-robinhood.txt"
                ) from exc
        self.rh = api
        self._credentials = (username, password, mfa_secret)
        self.account_number = account_number or None
        self._option_ids: dict[str, str] = {}
        self._logged_in_at = 0.0
        self._login()
        log.warning(
            "Connected to Robinhood (unofficial API). There is no paper trading: orders are REAL MONEY."
        )

    # ------------------------------------------------------------------ session
    def _login(self) -> None:
        username, password, mfa_secret = self._credentials
        mfa_code = None
        if mfa_secret:
            import pyotp

            mfa_code = pyotp.TOTP(mfa_secret).now()
        # store_session reuses the saved token, so restarts don't trigger a new verification prompt.
        result = self.rh.login(username, password, expiresIn=86400, store_session=True, mfa_code=mfa_code)
        if not result or "access_token" not in result:
            raise RuntimeError(f"Robinhood login failed: {result}")
        self._logged_in_at = time.monotonic()

    def _session(self) -> None:
        # Tokens last 24 hours; refresh well before that so a long-running bot doesn't get logged out.
        if time.monotonic() - self._logged_in_at > _RELOGIN_SECONDS:
            log.info("Refreshing Robinhood login")
            self._login()

    # ------------------------------------------------------------------ option contract lookup
    def _option_id(self, symbol: str) -> str:
        if symbol in self._option_ids:
            return self._option_ids[symbol]
        root, expiration, right, strike = parse_occ(symbol)
        option_type = "call" if right == "C" else "put"
        params = {
            "expiration_dates": expiration.isoformat(),
            "strike_price": f"{strike:.4f}",
            "type": option_type,
            "state": "active",
        }
        # Look up by chain symbol rather than robin_stocks' stock-instrument lookup, which has no
        # entry for an index like SPX.
        for chain in [root] + ([_ROOT_ALIASES[root]] if root in _ROOT_ALIASES else []):
            data = self.rh.helper.request_get(
                self.rh.urls.option_instruments_url(), "pagination", {**params, "chain_symbol": chain}
            )
            for item in data or []:
                if (
                    (item.get("chain_symbol") or "").upper() == chain
                    and item.get("expiration_date") == expiration.isoformat()
                    and item.get("type") == option_type
                    and abs(float(item.get("strike_price", 0)) - strike) < 1e-6
                ):
                    self._option_ids[symbol] = item["id"]
                    return item["id"]
        raise LookupError(f"Robinhood has no active contract for {symbol}")

    def _option_quote(self, symbol: str) -> dict:
        url = self.rh.urls.option_instruments_url(self._option_id(symbol))
        data = self.rh.helper.request_get(
            self.rh.urls.marketdata_options_url(), "results", {"instruments": url}
        )
        quote = data[0] if isinstance(data, list) and data else data
        if not quote:
            raise LookupError(f"no Robinhood quote for {symbol}")
        return quote

    # ------------------------------------------------------------------ Broker interface
    def buying_power(self) -> float:
        self._session()
        return float(
            self.rh.profiles.load_account_profile(account_number=self.account_number, info="buying_power")
        )

    def latest_price(self, symbol: str, asset_type: str) -> float | None:
        self._session()
        try:
            if asset_type == OPTION:
                q = self._option_quote(symbol)
                bid, ask = float(q.get("bid_price") or 0), float(q.get("ask_price") or 0)
                if bid and ask:
                    return (bid + ask) / 2
                return float(q.get("mark_price") or 0) or None
            return float(self.rh.stocks.get_latest_price(symbol)[0])
        except Exception as exc:  # noqa: BLE001 - data outages should not crash the bot
            log.warning("Could not fetch a Robinhood price for %s: %s", symbol, exc)
            return None

    def position_qty(self, symbol: str) -> int:
        self._session()
        if is_occ_symbol(symbol):
            option_id = self._option_id(symbol)
            positions = self.rh.options.get_open_option_positions(account_number=self.account_number)
            return int(
                sum(
                    float(p["quantity"])
                    for p in positions or []
                    if p.get("option_id") == option_id and p.get("type", "long") == "long"
                )
            )
        positions = self.rh.account.get_open_stock_positions(account_number=self.account_number)
        total = 0.0
        for p in positions or []:
            pos_symbol = p.get("symbol") or self.rh.stocks.get_symbol_by_url(p["instrument"])
            if pos_symbol == symbol:
                total += float(p["quantity"])
        return int(total)

    def cancel_open_orders(self, symbol: str) -> int:
        self._session()
        cancelled = 0
        if is_occ_symbol(symbol):
            option_id = self._option_id(symbol)
            for order in self.rh.orders.get_all_open_option_orders(account_number=self.account_number) or []:
                if any(leg.get("option", "").rstrip("/").endswith(option_id) for leg in order.get("legs", [])):
                    self.rh.orders.cancel_option_order(order["id"])
                    cancelled += 1
            return cancelled
        for order in self.rh.orders.get_all_open_stock_orders(account_number=self.account_number) or []:
            if self.rh.stocks.get_symbol_by_url(order["instrument"]) == symbol:
                self.rh.orders.cancel_stock_order(order["id"])
                cancelled += 1
        return cancelled

    def submit_order(self, symbol, qty, side, asset_type, limit_price=None):
        self._session()
        if asset_type == OPTION:
            return self._submit_option(symbol, qty, side, limit_price)
        acct = self.account_number
        if limit_price is not None:
            fn = self.rh.orders.order_buy_limit if side == "buy" else self.rh.orders.order_sell_limit
            resp = fn(symbol, qty, limit_price, account_number=acct, timeInForce="gfd")
        else:
            fn = self.rh.orders.order_buy_market if side == "buy" else self.rh.orders.order_sell_market
            resp = fn(symbol, qty, account_number=acct, timeInForce="gfd")
        return self._result(resp, symbol, side, qty, limit_price)

    def _submit_option(self, symbol, qty, side, limit_price):
        price = limit_price
        if price is None:
            # Options go in as limit orders. A "market" request becomes a limit at the far side of
            # the quote (the ask to buy, the bid to sell), which fills right away like a market order.
            q = self._option_quote(symbol)
            raw = float(q.get("ask_price" if side == "buy" else "bid_price") or 0)
            price = round_to_tick(raw or 0.01, symbol, OPTION, side)
        payload = {
            "account": self.rh.profiles.load_account_profile(account_number=self.account_number, info="url"),
            "direction": "debit" if side == "buy" else "credit",
            "time_in_force": "gfd",
            "legs": [
                {
                    "position_effect": "open" if side == "buy" else "close",
                    "side": side,
                    "ratio_quantity": 1,
                    "option": self.rh.urls.option_instruments_url(self._option_id(symbol)),
                }
            ],
            "type": "limit",
            "trigger": "immediate",
            "price": f"{price:.2f}",
            "quantity": qty,
            "override_day_trade_checks": False,
            "override_dtbp_checks": False,
            "ref_id": str(uuid4()),
        }
        url = self.rh.urls.option_orders_url(account_number=self.account_number)
        resp = self.rh.helper.request_post(url, payload, json=True)
        return self._result(resp, symbol, side, qty, price)

    @staticmethod
    def _result(resp, symbol, side, qty, price) -> OrderResult:
        if not isinstance(resp, dict) or not resp.get("id"):
            raise RuntimeError(f"Robinhood rejected the {side} order for {symbol}: {resp}")
        return OrderResult(str(resp["id"]), symbol, side, qty, price)
