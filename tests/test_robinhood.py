"""RobinhoodBroker against a fake robin_stocks module (no network, no real account)."""

from datetime import date
from types import SimpleNamespace

import pytest

from copytrader.broker import make_broker
from copytrader.broker_robinhood import RobinhoodBroker
from copytrader.config import Config
from copytrader.trader import Trader

SPXW = "SPXW260925C05800000"
INSTRUMENTS = "https://api.robinhood.com/options/instruments/"


class FakeRH:
    def __init__(self, chain_symbol="SPXW"):
        self.chain_symbol = chain_symbol
        self.posted = []
        self.cancelled = []
        self.positions = []
        self.open_orders = []
        self.quote = {"bid_price": "3.10", "ask_price": "3.30", "mark_price": "3.20"}
        self.reject = False
        self.logins = 0
        rh = self
        self.helper = SimpleNamespace(request_get=self._get, request_post=self._post)
        self.urls = SimpleNamespace(
            option_instruments_url=lambda id=None: f"{INSTRUMENTS}{id}/" if id else INSTRUMENTS,
            marketdata_options_url=lambda: "https://api.robinhood.com/marketdata/options/",
            option_orders_url=lambda account_number=None: "https://api.robinhood.com/options/orders/",
        )
        self.profiles = SimpleNamespace(
            load_account_profile=lambda account_number=None, info=None: {
                "buying_power": "5000.00",
                "url": "https://api.robinhood.com/accounts/ABC/",
            }[info]
        )
        self.options = SimpleNamespace(get_open_option_positions=lambda account_number=None: rh.positions)
        self.orders = SimpleNamespace(
            get_all_open_option_orders=lambda account_number=None: rh.open_orders,
            cancel_option_order=lambda oid: rh.cancelled.append(oid),
        )

    def login(self, username, password, **kw):
        self.logins += 1
        return {"access_token": "t"}

    def _get(self, url, data_type="regular", payload=None):
        if url == INSTRUMENTS:
            if payload["chain_symbol"] != self.chain_symbol:
                return []
            return [
                {
                    "id": "opt-1",
                    "chain_symbol": self.chain_symbol,
                    "expiration_date": payload["expiration_dates"],
                    "strike_price": payload["strike_price"],
                    "type": payload["type"],
                }
            ]
        if "marketdata" in url:
            return [self.quote]
        raise AssertionError(url)

    def _post(self, url, payload, json=False):
        self.posted.append(payload)
        if self.reject:
            return {"detail": "Not enough buying power."}
        # Simulate an immediate fill so position_qty sees it.
        leg = payload["legs"][0]
        sign = 1 if leg["side"] == "buy" else -1
        self.positions = [{"option_id": "opt-1", "type": "long", "quantity": str(
            sum(float(p["quantity"]) for p in self.positions) + sign * payload["quantity"])}]
        return {"id": f"order-{len(self.posted)}"}


@pytest.fixture
def rh():
    return FakeRH()


def broker(rh):
    return RobinhoodBroker("user", "pw", api=rh)


def test_limit_buy_payload(rh):
    b = broker(rh)
    result = b.submit_order(SPXW, 2, "buy", "option", limit_price=3.40)
    p = rh.posted[-1]
    assert result.order_id == "order-1"
    assert (p["direction"], p["type"], p["price"], p["quantity"], p["time_in_force"]) == (
        "debit", "limit", "3.40", 2, "gfd")
    assert p["legs"][0] == {
        "position_effect": "open", "side": "buy", "ratio_quantity": 1, "option": f"{INSTRUMENTS}opt-1/"}


def test_market_sell_uses_bid_rounded_down(rh):
    b = broker(rh)
    rh.quote = {"bid_price": "3.17", "ask_price": "3.40"}
    b.submit_order(SPXW, 1, "sell", "option")
    p = rh.posted[-1]
    assert p["price"] == "3.10" and p["direction"] == "credit"
    assert p["legs"][0]["position_effect"] == "close"


def test_spx_listed_under_index_symbol(rh):
    rh.chain_symbol = "SPX"
    assert broker(rh)._option_id(SPXW) == "opt-1"


def test_unknown_contract(rh):
    rh.chain_symbol = "OTHER"
    with pytest.raises(LookupError):
        broker(rh)._option_id(SPXW)


def test_positions_and_cancel(rh):
    b = broker(rh)
    rh.positions = [
        {"option_id": "opt-1", "type": "long", "quantity": "3.0000"},
        {"option_id": "opt-2", "type": "long", "quantity": "5.0000"},
    ]
    assert b.position_qty(SPXW) == 3
    rh.open_orders = [
        {"id": "o1", "legs": [{"option": f"{INSTRUMENTS}opt-1/"}]},
        {"id": "o2", "legs": [{"option": f"{INSTRUMENTS}opt-2/"}]},
    ]
    assert b.cancel_open_orders(SPXW) == 1 and rh.cancelled == ["o1"]


def test_mid_price(rh):
    assert broker(rh).latest_price(SPXW, "option") == pytest.approx(3.20)


def test_rejection_raises(rh):
    rh.reject = True
    with pytest.raises(RuntimeError, match="buying power"):
        broker(rh).submit_order(SPXW, 1, "buy", "option", limit_price=3.4)


def test_paper_mode_refused_for_robinhood():
    cfg = Config(dry_run=False)
    cfg.broker.name = "robinhood"
    with pytest.raises(SystemExit, match="no paper trading"):
        make_broker(cfg)


def test_copy_trade_round_trip(rh, tmp_path):
    cfg = Config(state_file=str(tmp_path / "s.json"))
    cfg.sizing.mode, cfg.sizing.fixed_contracts = "fixed", 1
    trader = Trader(cfg, broker(rh), today=lambda: date(2026, 9, 25))
    [entry] = trader.handle_message(1, "In SPX 5800C 3.20")
    assert entry.status == "ordered" and rh.posted[-1]["price"] == "3.40"
    [exit_] = trader.handle_message(2, "Out SPX")
    assert exit_.status == "ordered"
    assert rh.posted[-1]["legs"][0]["side"] == "sell" and rh.posted[-1]["price"] == "3.10"
    assert trader.ledger.positions == {}
