"""Behaviour specific to SPX 0DTE channels."""

from datetime import date, datetime

import pytest

from copytrader.broker import DryRunBroker
from copytrader.config import Config, load_config
from copytrader.trader import Trader, round_to_tick

TODAY = date(2026, 9, 25)
SPXW = "SPXW260925C05800000"


@pytest.fixture
def setup(tmp_path):
    cfg = Config(state_file=str(tmp_path / "state.json"))
    cfg.sizing.mode = "fixed"
    cfg.sizing.fixed_contracts = 2
    cfg.risk.max_dollars_per_trade = 2000
    broker = DryRunBroker(starting_cash=10_000)
    return cfg, broker, Trader(cfg, broker, today=lambda: TODAY)


def test_spx_without_date_is_0dte_spxw(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "In SPX 5800C 3.20")
    assert out.status == "ordered"
    order = broker.orders[-1]
    # 3.20 * 1.05 = 3.36, rounded up to the $0.10 SPX tick
    assert (order.symbol, order.qty, order.limit_price) == (SPXW, 2, 3.40)


def test_spx_exit_matches_position(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "In SPX 5800C 3.20")
    trader.handle_message(2, "Trim SPX 5800C")
    assert broker.orders[-1].qty == 1
    trader.handle_message(3, "Out SPX")
    assert broker.orders[-1].side == "sell" and broker.orders[-1].qty == 1
    assert trader.ledger.positions == {}


def test_spx_without_strike_is_skipped(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "Buying SPX here")
    assert out.status == "skipped" and "index" in out.detail
    assert broker.orders == []


def test_non_index_option_still_needs_date(setup):
    cfg, broker, trader = setup
    [out] = trader.handle_message(1, "BTO SPY 580C @ 1.20")
    assert out.status == "skipped" and "expiration" in out.detail


def test_expensive_contract_capped(setup):
    cfg, broker, trader = setup
    cfg.risk.max_dollars_per_trade = 500
    [out] = trader.handle_message(1, "In SPX 5800C 8.00")
    assert out.status == "skipped" and "budget" in out.detail


@pytest.mark.parametrize(
    "price, symbol, side, expected",
    [
        (1.234, SPXW, "buy", 1.25),
        (1.234, SPXW, "sell", 1.20),
        (3.36, SPXW, "buy", 3.40),
        (3.36, SPXW, "sell", 3.30),
        (1.26, "SPY261020C00450000", "buy", 1.26),
        (118.123, "NVDA", "buy", 118.13),
        (0.01, SPXW, "sell", 0.05),
    ],
)
def test_round_to_tick(price, symbol, side, expected):
    asset = "stock" if symbol == "NVDA" else "option"
    assert round_to_tick(price, symbol, asset, side) == pytest.approx(expected)


def test_auto_close_before_expiry(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "In SPX 5800C 3.20")
    trader.handle_message(2, "BTO SPX 5900C 10/2 @ 2.00")
    assert trader.close_expiring(datetime(2026, 9, 25, 15, 49)) == []
    outcomes = trader.close_expiring(datetime(2026, 9, 25, 15, 50))
    assert len(outcomes) == 1 and outcomes[0].status == "ordered"
    assert broker.orders[-1].symbol == SPXW and broker.orders[-1].side == "sell"
    assert list(trader.ledger.positions) == ["SPXW261002C05900000"]


def test_auto_close_can_be_disabled(setup):
    cfg, broker, trader = setup
    cfg.execution.auto_close_expiring_at = None
    trader.handle_message(1, "In SPX 5800C 3.20")
    assert trader.close_expiring(datetime(2026, 9, 25, 16, 0)) == []


def test_unquoted_yaml_time(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("execution:\n  auto_close_expiring_at: 15:45\n")
    cfg = load_config(path)
    trader = Trader(cfg, DryRunBroker(), today=lambda: TODAY)
    trader.ledger.positions[SPXW] = {
        "ticker": "SPX", "asset_type": "option", "strike": 5800.0, "right": "C",
        "expiration": "2026-09-25", "qty": 1,
    }
    trader.broker.positions[SPXW] = 1
    assert trader.close_expiring(datetime(2026, 9, 25, 15, 44)) == []
    assert len(trader.close_expiring(datetime(2026, 9, 25, 15, 45))) == 1


def test_stale_expired_position_cleared_any_time(setup):
    cfg, broker, trader = setup
    old = "SPXW260924C05800000"
    trader.ledger.positions[old] = {
        "ticker": "SPX", "asset_type": "option", "strike": 5800.0, "right": "C",
        "expiration": "2026-09-24", "qty": 1,
    }
    [out] = trader.close_expiring(datetime(2026, 9, 25, 9, 30))
    assert out.status == "skipped"  # the broker no longer holds it, so the ledger entry is dropped
    assert trader.ledger.positions == {}


def test_failed_auto_close_backs_off(setup):
    cfg, broker, trader = setup
    trader.handle_message(1, "In SPX 5800C 3.20")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("market closed")

    broker.submit_order = boom
    assert trader.close_expiring(datetime(2026, 9, 25, 16, 0))[0].status == "error"
    assert trader.close_expiring(datetime(2026, 9, 25, 16, 2)) == []
    assert trader.close_expiring(datetime(2026, 9, 25, 16, 6))[0].status == "error"
    assert len(calls) == 2
