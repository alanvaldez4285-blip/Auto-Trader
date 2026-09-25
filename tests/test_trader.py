from datetime import date

import pytest

from copytrader.broker import DryRunBroker
from copytrader.config import Config
from copytrader.trader import Ledger, Trader

TODAY = date(2026, 9, 25)
CALL = "SPY261020C00450000"


@pytest.fixture
def setup(tmp_path):
    cfg = Config(state_file=str(tmp_path / "state.json"))
    broker = DryRunBroker(starting_cash=10_000)
    trader = Trader(cfg, broker, today=lambda: TODAY)
    return cfg, broker, trader


def test_option_entry_sized_by_dollars(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "BTO SPY 450c 10/20 @ 1.20")
    assert out.status == "ordered"
    # $500 / (1.20 * 1.05 * 100 = $126) -> 3 contracts, limit 1.26
    order = broker.orders[-1]
    assert (order.symbol, order.side, order.qty, order.limit_price) == (CALL, "buy", 3, 1.26)
    assert trader.ledger.positions[CALL]["qty"] == 3


def test_full_exit_with_abbreviated_message(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "BTO SPY 450c 10/20 @ 1.20")
    [out] = trader.handle_message(2, "STC SPY 450c @ 1.80")
    assert out.status == "ordered"
    assert broker.orders[-1].side == "sell" and broker.orders[-1].qty == 3
    assert broker.orders[-1].limit_price is None  # market exit by default
    assert CALL not in trader.ledger.positions


def test_trim_then_exit(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "BTO SPY 450c 10/20 @ 1.20")
    trader.handle_message(2, "Trim SPY 450c")
    assert broker.orders[-1].qty == 1  # floor(3 * 0.5)
    assert trader.ledger.positions[CALL]["qty"] == 2
    trader.handle_message(3, "all out SPY")
    assert broker.orders[-1].qty == 2
    assert trader.ledger.positions == {}


def test_exit_never_sells_positions_bot_did_not_open(setup):
    cfg, broker, trader = setup
    broker.positions["AAPL"] = 50  # the user's own shares
    [out] = trader.handle_message(1, "Sold all AAPL")
    assert out.status == "skipped"
    assert broker.orders == []


def test_exit_sells_only_bot_quantity(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "Buy NVDA @ 100")
    bot_qty = trader.ledger.positions["NVDA"]["qty"]
    broker.positions["NVDA"] += 20  # user bought more manually
    trader.handle_message(2, "Sold NVDA")
    assert broker.orders[-1].qty == bot_qty


def test_duplicate_message_ignored(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "Buy NVDA @ 100")
    assert trader.handle_message(1, "Buy NVDA @ 100") == []
    assert len(broker.orders) == 1


def test_no_double_entry(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "Buy NVDA @ 100")
    [out] = trader.handle_message(2, "Buy NVDA @ 101")
    assert out.status == "skipped" and len(broker.orders) == 1


def test_daily_trade_limit(setup):
    cfg, broker, trader = setup
    cfg.risk.max_trades_per_day = 1
    trader.handle_message(1, "Buy NVDA @ 10")
    [out] = trader.handle_message(2, "Buy AMD @ 10")
    assert out.status == "skipped" and "daily limit" in out.detail


def test_too_expensive_contract_skipped(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "BTO SPY 450c 10/20 @ 12.00")
    assert out.status == "skipped"
    assert broker.orders == []


def test_option_without_expiration_not_bought(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "BTO SPY 450c @ 1.20")
    assert out.status == "skipped" and "expiration" in out.detail


def test_ticker_allow_list(setup):
    cfg, broker, trader = setup
    cfg.risk.allowed_tickers = ["SPY"]
    [out] = trader.handle_message(1, "Buy NVDA @ 100")
    assert out.status == "skipped"


def test_market_price_used_when_message_has_none(setup):
    cfg, broker, trader = setup
    broker.prices["NVDA"] = 100.0
    [out] = trader.handle_message(1, "Buy NVDA")
    assert out.status == "ordered"
    assert broker.orders[-1].qty == 4  # $500 / $105


def test_unfilled_entry_cleared_on_exit(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "Buy NVDA @ 100")
    broker.positions.clear()  # simulate the limit order never filling
    [out] = trader.handle_message(2, "Sold NVDA")
    assert out.status == "skipped" and "never filled" in out.detail
    assert trader.ledger.positions == {}


def test_state_persists(setup, tmp_path):
    cfg, broker, trader = setup
    trader.handle_message(1, "Buy NVDA @ 100")
    reloaded = Ledger(cfg.state_file)
    assert "NVDA" in reloaded.positions
    assert "1" in reloaded.processed
    assert reloaded.entries_today(TODAY) == 1
