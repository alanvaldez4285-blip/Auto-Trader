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


@pytest.mark.parametrize(
    "text, action",
    [
        ("In SPX 5800C 3.20", BUY),
        ("im in spx 5850p @ 4.1", BUY),
        ("Out SPX", SELL),
        ("Getting out SPX 5800C here", SELL),
    ],
)
def test_line_start_in_out(text, action):
    s = p(text)
    assert s.action == action and s.ticker == "SPX"


def test_in_mid_sentence_is_not_a_buy():
    assert p("SPX looking good in here") is None
    assert p("In a meeting") is None


def test_action_word_at_end():
    s = p("SPX 5800C 0DTE @ 3.20 BTO")
    assert (s.action, s.ticker, s.strike, s.expiration, s.price) == (BUY, "SPX", 5800, TODAY, 3.20)


def test_extra_words():
    assert parse_signal("Loading SPX 5800c 2.5", TODAY) is None
    s = parse_signal("Loading SPX 5800c 2.5", TODAY, {"buy": ["loading"]})
    assert s.action == BUY and s.price == 2.5
    s = parse_signal("Paid out SPX", TODAY, {"sell": ["paid out"]})
    assert s.action == SELL


def test_spx_maps_to_spxw():
    s = p("BTO SPX 5800C 9/25 @ 3.20")
    assert s.symbol == "SPXW260925C05800000"
    assert s.broker_symbol({}) == "SPX260925C05800000"


def test_implicit_option_entry():
    text = "QCOM 205C at 1.00 - lotto @everyone"
    assert p(text) is None  # off by default
    s = parse_signal(text, TODAY, implicit_buy=True)
    assert (s.action, s.ticker, s.strike, s.right, s.price) == (BUY, "QCOM", 205, "C", 1.00)
    s = parse_signal("DELL 575C at .40 - lotto @everyone", TODAY, implicit_buy=True)
    assert (s.ticker, s.strike, s.price) == ("DELL", 575, 0.40)


@pytest.mark.parametrize("text", ["300% @everyone", "Let's see @everyone", "NVDA looking strong at 120"])
def test_implicit_entry_needs_full_contract_and_price(text):
    assert parse_signal(text, TODAY, implicit_buy=True) is None


@pytest.mark.parametrize(
    "text, action",
    [("Sold", SELL), ("Out", SELL), ("stopped out", SELL), ("Cutting this", SELL),
     ("Trimmed some here", TRIM), ("sell some if you want", TRIM), ("550%", None)],
)
def test_detect_action(text, action):
    from copytrader.parser import detect_action

    assert detect_action(text) == action
