"""Alerts in philthekeys' style: no action word, no date, exits as replies."""

from datetime import date

import pytest

from copytrader.broker import DryRunBroker
from copytrader.config import Config, load_config
from copytrader.trader import Trader

ALERT = "QCOM 205C at 1.00 - lotto @everyone"
QCOM = "QCOM260925C00205000"


def make(tmp_path, today=date(2026, 9, 25)):
    cfg = load_config("examples/phil-trades.yaml")
    cfg.state_file = str(tmp_path / "state.json")
    broker = DryRunBroker(starting_cash=5_000)
    return cfg, broker, Trader(cfg, broker, today=lambda: today)


def test_entry_then_updates_then_reply_exit(tmp_path):
    cfg, broker, trader = make(tmp_path)
    [out] = trader.handle_message(1, ALERT)
    assert out.status == "ordered"
    order = broker.orders[-1]
    # $200 / (1.00 * 1.10 * 100) = 1 contract, limit 1.10
    assert (order.symbol, order.qty, order.limit_price) == (QCOM, 1, 1.10)

    assert trader.handle_message(2, "Let's see @everyone", reply_to=ALERT) == []
    assert trader.handle_message(3, "300% @everyone", reply_to=ALERT) == []
    assert len(broker.orders) == 1

    [out] = trader.handle_message(4, "Sold", reply_to=ALERT)
    assert out.status == "ordered" and broker.orders[-1].side == "sell"
    assert trader.ledger.positions == {}


def test_reply_trim(tmp_path):
    cfg, broker, trader = make(tmp_path)
    alert = "DELL 575C at .40 - lotto"
    trader.handle_message(1, alert)
    assert broker.orders[-1].qty == 4  # $200 / $44
    trader.handle_message(2, "trimmed some", reply_to=alert)
    assert broker.orders[-1].qty == 2
    trader.handle_message(3, "out", reply_to=alert)
    assert broker.orders[-1].qty == 2 and trader.ledger.positions == {}


def test_reply_restating_contract_is_not_a_new_entry(tmp_path):
    cfg, broker, trader = make(tmp_path)
    assert trader.handle_message(1, "QCOM 205C at 2.50 now", reply_to=ALERT) == []
    assert broker.orders == []


def test_reply_exit_days_later_still_matches(tmp_path):
    cfg, broker, trader = make(tmp_path, today=date(2026, 9, 21))  # Monday
    trader.handle_message(1, ALERT)
    assert broker.orders[-1].symbol == QCOM  # Monday alert -> that week's Friday
    trader._clock = lambda: date(2026, 9, 23)
    [out] = trader.handle_message(2, "Sold", reply_to=ALERT)
    assert out.status == "ordered"


@pytest.mark.parametrize(
    "today, expected",
    [(date(2026, 9, 21), "260925"), (date(2026, 9, 25), "260925"), (date(2026, 9, 26), "261002")],
)
def test_friday_expiration(tmp_path, today, expected):
    cfg, broker, trader = make(tmp_path, today=today)
    trader.handle_message(1, ALERT)
    assert broker.orders[-1].symbol == f"QCOM{expected}C00205000"


def test_missing_expiration_skip_by_default(tmp_path):
    cfg = Config(state_file=str(tmp_path / "s.json"))
    cfg.parsing.implicit_option_entries = True
    trader = Trader(cfg, DryRunBroker(), today=lambda: date(2026, 9, 25))
    [out] = trader.handle_message(1, ALERT)
    assert out.status == "skipped" and "expiration" in out.detail
