from datetime import date

import pytest

from copytrader.parser import BUY, OPTION, SELL, STOCK, TRIM, occ_symbol, parse_signal

TODAY = date(2026, 9, 25)


def p(text):
    return parse_signal(text, today=TODAY)


@pytest.mark.parametrize(
    "text, action, ticker, strike, right, exp, price",
    [
        ("BTO SPY 450c 10/20 @ 1.20", BUY, "SPY", 450, "C", date(2026, 10, 20), 1.20),
        ("BTO $TSLA 12/15 250P 1.50  SL 1.00", BUY, "TSLA", 250, "P", date(2026, 12, 15), 1.50),
        ("STC SPY 450c 10/20 @ 1.80", SELL, "SPY", 450, "C", date(2026, 10, 20), 1.80),
        ("bto qqq 0dte 480p .85", BUY, "QQQ", 480, "P", TODAY, 0.85),
        ("BTO SPY 2026-10-20 450C @ 1.2", BUY, "SPY", 450, "C", date(2026, 10, 20), 1.2),
        ("**BTO** $SPY 580C 10/3 @ .95", BUY, "SPY", 580, "C", date(2026, 10, 3), 0.95),
        ("BTO AAPL 1/17 200c 3.10", BUY, "AAPL", 200, "C", date(2027, 1, 17), 3.10),
        ("BTO AAPL 1/17/28 200 calls @ 3.10", BUY, "AAPL", 200, "C", date(2028, 1, 17), 3.10),
        ("BTO SPY 452.5c 10/20 @ 1.05", BUY, "SPY", 452.5, "C", date(2026, 10, 20), 1.05),
    ],
)
def test_options(text, action, ticker, strike, right, exp, price):
    s = p(text)
    assert s is not None
    assert (s.action, s.ticker, s.asset_type) == (action, ticker, OPTION)
    assert (s.strike, s.right, s.expiration, s.price) == (strike, right, exp, price)


@pytest.mark.parametrize(
    "text, action, ticker, price",
    [
        ("Buy NVDA @ 118.50", BUY, "NVDA", 118.50),
        ("ok guys buy AMD at 150 PT 170", BUY, "AMD", 150.0),
        ("Sold all AAPL", SELL, "AAPL", None),
        ("Sell half TSLA", TRIM, "TSLA", None),
        ("Trimming some $PLTR here 25.40", TRIM, "PLTR", 25.40),
        ("Entry $SOFI 7.85 stop 7.50", BUY, "SOFI", 7.85),
        ("STC SPY", SELL, "SPY", None),
    ],
)
def test_stocks(text, action, ticker, price):
    s = p(text)
    assert s is not None
    assert (s.action, s.ticker, s.asset_type, s.price) == (action, ticker, STOCK, price)


def test_trim_option_without_expiration():
    s = p("Trim SPY 450c")
    assert (s.action, s.asset_type, s.strike, s.expiration) == (TRIM, OPTION, 450, None)
    assert not s.is_complete_contract


@pytest.mark.parametrize(
    "text",
    ["good morning everyone", "long day today", "", "SPY looking strong", "https://example.com/buy"],
)
def test_not_signals(text):
    assert p(text) is None


def test_mentions_and_emoji_ignored():
    s = p("<@&12345> <:rocket:998877> BTO $SPY 450c 10/20 @ 1.20")
    assert s.ticker == "SPY" and s.price == 1.20


def test_stop_loss_is_not_price():
    s = p("BTO SPY 450c 10/20 SL .80")
    assert s.price is None


def test_occ_symbol():
    assert occ_symbol("spy", date(2026, 10, 20), "c", 450) == "SPY261020C00450000"
    assert occ_symbol("SPY", date(2026, 10, 20), "P", 452.5) == "SPY261020P00452500"
