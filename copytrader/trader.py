"""Core copy-trading logic: signal -> risk checks -> sizing -> order, with a persistent position ledger.

The ledger records only positions this bot opened. Exit signals sell from the ledger, so shares or
contracts you bought yourself in the same account are never touched.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import Broker
from .config import Config, parse_hhmm
from .parser import BUY, INDEX_TICKERS, OPTION, SELL, STOCK, TRIM, Signal, parse_signal

log = logging.getLogger(__name__)

MAX_REMEMBERED_MESSAGES = 2000
# Cboe minimum price increments for index options: $0.05 under $3, $0.10 at $3 and above.
_NICKEL_DIME_ROOTS = {"SPX", "SPXW", "VIX", "VIXW", "NDX", "NDXP", "RUT", "RUTW", "DJX"}


def option_root(symbol: str) -> str:
    """Root of an OCC symbol: everything before the 6-digit date, e.g. SPXW from SPXW260925C06500000."""
    return symbol[:-15]


def round_to_tick(price: float, symbol: str, asset_type: str, side: str) -> float:
    if asset_type == OPTION and option_root(symbol) in _NICKEL_DIME_ROOTS:
        tick = 0.05 if price < 3 else 0.10
    else:
        tick = 0.01
    steps = price / tick
    # Buys round up and sells round down, so the limit is never tighter than intended.
    steps = math.ceil(steps - 1e-9) if side == "buy" else math.floor(steps + 1e-9)
    return round(max(steps, 1) * tick, 2)


@dataclass
class Outcome:
    status: str  # "ordered", "skipped" or "error"
    detail: str

    def __str__(self) -> str:
        return f"[{self.status}] {self.detail}"


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.positions: dict[str, dict] = {}
        self.trades_date = ""
        self.trades_count = 0
        self.processed: list[str] = []
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.positions = data.get("positions", {})
            self.trades_date = data.get("trades_date", "")
            self.trades_count = data.get("trades_count", 0)
            self.processed = data.get("processed", [])

    def save(self) -> None:
        data = {
            "positions": self.positions,
            "trades_date": self.trades_date,
            "trades_count": self.trades_count,
            "processed": self.processed[-MAX_REMEMBERED_MESSAGES:],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def entries_today(self, today: date) -> int:
        return self.trades_count if self.trades_date == today.isoformat() else 0

    def count_entry(self, today: date) -> None:
        if self.trades_date != today.isoformat():
            self.trades_date, self.trades_count = today.isoformat(), 0
        self.trades_count += 1


class Trader:
    def __init__(self, cfg: Config, broker: Broker, ledger: Ledger | None = None, today=None):
        self.cfg = cfg
        self.broker = broker
        self.ledger = ledger or Ledger(cfg.state_file)
        self._clock = today
        self._lock = threading.Lock()
        self._auto_close_failed_at: dict[str, datetime] = {}

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg.execution.market_timezone))

    def _today(self) -> date:
        # Expirations and daily limits follow the exchange's calendar day, not the local one.
        return self._clock() if self._clock else self._now().date()

    def parse(self, text: str) -> Signal | None:
        """Parse with this config's extra words, filling in the 0DTE expiration where configured."""
        today = self._today()
        signal = parse_signal(text, today=today, extra_words=self.cfg.parsing.extra_words())
        if (
            signal
            and signal.action == BUY
            and signal.asset_type == OPTION
            and signal.expiration is None
            and signal.ticker in self.cfg.parsing.assume_0dte_tickers
        ):
            signal.expiration = today
        return signal

    # ------------------------------------------------------------------ entry point
    def handle_message(self, message_id: str | int, text: str) -> list[Outcome]:
        with self._lock:
            message_id = str(message_id)
            if message_id in self.ledger.processed:
                return []
            self.ledger.processed.append(message_id)
            signal = self.parse(text)
            if signal is None:
                self.ledger.save()
                return []
            log.info("Signal from message %s: %s", message_id, signal.describe())
            try:
                if signal.action == BUY:
                    outcomes = [self._enter(signal, message_id)]
                else:
                    outcomes = self._exit(signal)
            except Exception as exc:  # noqa: BLE001 - one bad order must not kill the listener
                log.exception("Failed to act on signal %s", signal.describe())
                outcomes = [Outcome("error", f"{signal.describe()}: {exc}")]
            self.ledger.save()
            for outcome in outcomes:
                (log.warning if outcome.status == "error" else log.info)("%s", outcome)
            return outcomes

    # ------------------------------------------------------------------ entries
    def _enter(self, signal: Signal, message_id: str) -> Outcome:
        risk, sizing, execution = self.cfg.risk, self.cfg.sizing, self.cfg.execution
        what = signal.describe()

        if signal.asset_type == STOCK and signal.ticker in INDEX_TICKERS:
            return Outcome("skipped", f"{what}: {signal.ticker} is an index; the message named no option strike")
        if signal.asset_type == STOCK and not risk.allow_stocks:
            return Outcome("skipped", f"{what}: stock trading is disabled")
        if signal.asset_type == OPTION and not risk.allow_options:
            return Outcome("skipped", f"{what}: options trading is disabled")
        if signal.asset_type == OPTION and not signal.is_complete_contract:
            return Outcome("skipped", f"{what}: option call-out is missing its expiration date")
        if risk.allowed_tickers and signal.ticker not in risk.allowed_tickers:
            return Outcome("skipped", f"{what}: {signal.ticker} is not in allowed_tickers")
        if signal.ticker in risk.blocked_tickers:
            return Outcome("skipped", f"{what}: {signal.ticker} is in blocked_tickers")
        if risk.require_price_for_entries and signal.price is None:
            return Outcome("skipped", f"{what}: no entry price in the message")
        today = self._today()
        if self.ledger.entries_today(today) >= risk.max_trades_per_day:
            return Outcome("skipped", f"{what}: daily limit of {risk.max_trades_per_day} entries reached")

        symbol = signal.broker_symbol(execution.option_roots)
        existing = self.ledger.positions.get(symbol)
        if existing and not risk.add_to_existing_positions:
            return Outcome("skipped", f"{what}: already holding {existing['qty']} {symbol}")
        if not existing and len(self.ledger.positions) >= risk.max_open_positions:
            return Outcome("skipped", f"{what}: max_open_positions ({risk.max_open_positions}) reached")

        price = signal.price or self.broker.latest_price(symbol, signal.asset_type)
        if not price or price <= 0:
            return Outcome("skipped", f"{what}: no price in the message and no market quote available")

        multiplier = 100 if signal.asset_type == OPTION else 1
        order_price = round_to_tick(
            price * (1 + execution.entry_slippage_pct / 100), symbol, signal.asset_type, "buy"
        )
        unit_cost = order_price * multiplier
        if sizing.mode == "fixed":
            qty = sizing.fixed_contracts if signal.asset_type == OPTION else sizing.fixed_shares
        else:
            qty = math.floor(sizing.dollars_per_trade / unit_cost)
        qty = min(qty, math.floor(risk.max_dollars_per_trade / unit_cost))
        if qty < 1:
            return Outcome(
                "skipped",
                f"{what}: one unit costs ${unit_cost:,.2f}, above your per-trade budget",
            )
        cost = qty * unit_cost
        buying_power = self.broker.buying_power()
        if cost > buying_power:
            return Outcome("skipped", f"{what}: needs ${cost:,.2f} but buying power is ${buying_power:,.2f}")

        limit = order_price if execution.entry_order_type == "limit" else None
        order = self.broker.submit_order(symbol, qty, "buy", signal.asset_type, limit_price=limit)

        self.ledger.count_entry(today)
        if existing:
            existing["qty"] += qty
        else:
            self.ledger.positions[symbol] = {
                "ticker": signal.ticker,
                "asset_type": signal.asset_type,
                "strike": signal.strike,
                "right": signal.right,
                "expiration": signal.expiration.isoformat() if signal.expiration else None,
                "qty": qty,
                "entry_price": price,
                "opened_at": datetime.now().isoformat(timespec="seconds"),
                "message_id": message_id,
            }
        price_text = f"limit ${limit:.2f}" if limit else "market"
        return Outcome("ordered", f"BUY {qty} {symbol} ({price_text}), order {order.order_id}")

    # ------------------------------------------------------------------ exits
    def _matching_positions(self, signal: Signal) -> list[str]:
        matches = []
        for symbol, pos in self.ledger.positions.items():
            if pos["ticker"] != signal.ticker:
                continue
            if signal.asset_type == OPTION:
                # Traders often abbreviate exits ("STC SPY 450c"); match on whatever details were given.
                if pos["asset_type"] != OPTION:
                    continue
                if signal.strike is not None and pos["strike"] != signal.strike:
                    continue
                if signal.right and pos["right"] != signal.right:
                    continue
                if signal.expiration and pos["expiration"] != signal.expiration.isoformat():
                    continue
            matches.append(symbol)
        return matches

    def _exit(self, signal: Signal) -> list[Outcome]:
        what = signal.describe()
        symbols = self._matching_positions(signal)
        if not symbols:
            return [Outcome("skipped", f"{what}: no position opened by this bot matches")]
        return [self._exit_one(signal, symbol) for symbol in symbols]

    def _exit_one(self, signal: Signal, symbol: str) -> Outcome:
        execution = self.cfg.execution
        pos = self.ledger.positions[symbol]
        what = f"{signal.action.upper()} {symbol}"

        # An unfilled entry order must not fill after we've been told to get out.
        cancelled = self.broker.cancel_open_orders(symbol)
        if cancelled:
            log.info("Cancelled %d open order(s) for %s", cancelled, symbol)

        held = min(pos["qty"], self.broker.position_qty(symbol))
        if held <= 0:
            del self.ledger.positions[symbol]
            return Outcome("skipped", f"{what}: entry order never filled, nothing to sell")

        if signal.action == TRIM:
            qty = math.floor(held * execution.trim_fraction)
            if qty < 1:
                return Outcome("skipped", f"{what}: holding {held}, too small to trim (waiting for full exit)")
        else:
            qty = held

        limit = None
        if execution.exit_order_type == "limit" and signal.price:
            limit = round_to_tick(
                signal.price * (1 - execution.exit_slippage_pct / 100), symbol, pos["asset_type"], "sell"
            )
        order = self.broker.submit_order(symbol, qty, "sell", pos["asset_type"], limit_price=limit)

        remaining = held - qty
        if remaining > 0:
            pos["qty"] = remaining
        else:
            del self.ledger.positions[symbol]
        price_text = f"limit ${limit:.2f}" if limit else "market"
        return Outcome("ordered", f"SELL {qty} {symbol} ({price_text}), order {order.order_id}")

    # ------------------------------------------------------------------ end-of-day safety net
    def close_expiring(self, now: datetime | None = None) -> list[Outcome]:
        """Market-sell bot positions in options that expire today, once the cut-off time has passed."""
        cutoff = self.cfg.execution.auto_close_expiring_at
        if not cutoff:
            return []
        now = now or self._now()
        past_cutoff = now.time() >= parse_hhmm(cutoff)
        today = now.date().isoformat()
        with self._lock:
            expiring = []
            for sym, pos in self.ledger.positions.items():
                exp = pos["asset_type"] == OPTION and pos["expiration"]
                # Contracts from earlier days (bot was offline) are cleared any time; today's after the cut-off.
                if not exp or exp > today or (exp == today and not past_cutoff):
                    continue
                failed_at = self._auto_close_failed_at.get(sym)
                if failed_at and (now - failed_at).total_seconds() < 300:
                    continue  # retry failed closes every 5 minutes, not every tick
                expiring.append((sym, pos))
            if not expiring:
                return []
            outcomes = []
            for symbol, pos in expiring:
                signal = Signal(action=SELL, ticker=pos["ticker"], asset_type=OPTION)
                try:
                    outcome = self._exit_one(signal, symbol)
                    outcome.detail = f"auto-close before expiry: {outcome.detail}"
                    self._auto_close_failed_at.pop(symbol, None)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Auto-close failed for %s", symbol)
                    self._auto_close_failed_at[symbol] = now
                    outcome = Outcome("error", f"auto-close {symbol}: {exc}")
                outcomes.append(outcome)
                (log.warning if outcome.status == "error" else log.info)("%s", outcome)
            self.ledger.save()
            return outcomes


__all__ = ["Trader", "Ledger", "Outcome", "BUY", "SELL", "TRIM"]
