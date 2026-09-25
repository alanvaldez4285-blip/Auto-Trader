"""Broker adapters. `DryRunBroker` simulates fills; `AlpacaBroker` places real (paper or live) orders."""

from __future__ import annotations

import itertools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .parser import OPTION

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: float | None


class Broker(ABC):
    @abstractmethod
    def buying_power(self) -> float: ...

    @abstractmethod
    def latest_price(self, symbol: str, asset_type: str) -> float | None: ...

    @abstractmethod
    def position_qty(self, symbol: str) -> int: ...

    @abstractmethod
    def cancel_open_orders(self, symbol: str) -> int: ...

    @abstractmethod
    def submit_order(
        self, symbol: str, qty: int, side: str, asset_type: str, limit_price: float | None = None
    ) -> OrderResult: ...


def round_price(price: float) -> float:
    """Round to whole cents; brokers reject sub-penny limit prices."""
    return max(round(price, 2), 0.01)


class DryRunBroker(Broker):
    """Logs orders and fills them instantly in memory. Nothing leaves your machine."""

    def __init__(self, starting_cash: float = 100_000.0):
        self.cash = starting_cash
        self.positions: dict[str, int] = {}
        self.prices: dict[str, float] = {}
        self.orders: list[OrderResult] = []
        self._ids = itertools.count(1)

    def buying_power(self) -> float:
        return self.cash

    def latest_price(self, symbol: str, asset_type: str) -> float | None:
        return self.prices.get(symbol)

    def position_qty(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def cancel_open_orders(self, symbol: str) -> int:
        return 0

    def submit_order(self, symbol, qty, side, asset_type, limit_price=None):
        multiplier = 100 if asset_type == OPTION else 1
        price = limit_price or self.prices.get(symbol, 0.0)
        signed = qty if side == "buy" else -qty
        self.positions[symbol] = self.positions.get(symbol, 0) + signed
        if self.positions[symbol] == 0:
            del self.positions[symbol]
        self.cash -= signed * price * multiplier
        result = OrderResult(f"dry-{next(self._ids)}", symbol, side, qty, limit_price)
        self.orders.append(result)
        return result


class AlpacaBroker(Broker):
    """Alpaca Markets (https://alpaca.markets). Supports stocks and options, paper and live."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        if not api_key or not secret_key:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)
        acct = self.trading.get_account()
        log.info(
            "Connected to Alpaca %s account %s (buying power $%s)",
            "PAPER" if paper else "LIVE",
            acct.account_number,
            acct.buying_power,
        )

    def buying_power(self) -> float:
        acct = self.trading.get_account()
        # Options can only be bought with cash-like buying power, never margin.
        options_bp = getattr(acct, "options_buying_power", None)
        return float(options_bp or acct.buying_power)

    def latest_price(self, symbol: str, asset_type: str) -> float | None:
        from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest

        try:
            if asset_type == OPTION:
                quote = self.option_data.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=symbol)
                )[symbol]
                if quote.ask_price and quote.bid_price:
                    return (quote.ask_price + quote.bid_price) / 2
                return quote.ask_price or None
            trade = self.stock_data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
            return float(trade[symbol].price)
        except Exception as exc:  # noqa: BLE001 - data outages should not crash the bot
            log.warning("Could not fetch a price for %s: %s", symbol, exc)
            return None

    def position_qty(self, symbol: str) -> int:
        from alpaca.common.exceptions import APIError

        try:
            return int(float(self.trading.get_open_position(symbol).qty))
        except APIError:
            return 0

    def cancel_open_orders(self, symbol: str) -> int:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        for order in orders:
            self.trading.cancel_order_by_id(order.id)
        return len(orders)

    def submit_order(self, symbol, qty, side, asset_type, limit_price=None):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        common = dict(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        if limit_price is not None:
            request = LimitOrderRequest(limit_price=round_price(limit_price), **common)
        else:
            request = MarketOrderRequest(**common)
        order = self.trading.submit_order(order_data=request)
        return OrderResult(str(order.id), symbol, side, qty, limit_price)


def make_broker(cfg) -> Broker:
    if cfg.dry_run:
        log.warning("DRY RUN: orders are simulated and never sent to a broker")
        return DryRunBroker()
    if cfg.broker.name == "robinhood":
        if cfg.broker.paper:
            raise SystemExit(
                "Robinhood has no paper trading. Test with dry_run: true first; set broker.paper: false "
                "to confirm you want real-money orders on Robinhood."
            )
        from .broker_robinhood import RobinhoodBroker

        log.warning("LIVE TRADING ENABLED on Robinhood via its unofficial API: real money orders")
        return RobinhoodBroker(
            cfg.broker.username,
            cfg.broker.password,
            mfa_secret=cfg.broker.mfa_secret,
            account_number=cfg.broker.account_number,
        )
    if cfg.broker.name != "alpaca":
        raise ValueError(f"unsupported broker {cfg.broker.name!r}")
    if not cfg.broker.paper:
        log.warning("LIVE TRADING ENABLED: real money orders will be placed")
    return AlpacaBroker(cfg.broker.api_key, cfg.broker.secret_key, paper=cfg.broker.paper)
