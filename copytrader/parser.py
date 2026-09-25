"""Turn free-form Discord trade call-outs into structured signals.

Handles the formats most signal servers use, for example:

    BTO SPY 450c 10/20 @ 1.20
    BTO $TSLA 12/15 250P 1.50  SL 1.00
    STC SPY 450c 10/20 @ 1.80
    Trim SPY 450c
    Buy NVDA @ 118.50
    Sold all AAPL
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

BUY, SELL, TRIM = "buy", "sell", "trim"
STOCK, OPTION = "stock", "option"

# Order matters only for readability; the earliest match in the message wins.
_ACTION_PATTERNS: list[tuple[str, str]] = [
    (TRIM, r"trim(?:ming|med)?|scal(?:e|ing)\s+out|sell(?:ing)?\s+(?:half|some)|sold\s+(?:half|some)"
           r"|partials?|taking\s+profits?|t(?:ook|aking)\s+some"),
    (BUY, r"bto|buy\s+to\s+open|buy(?:ing)?|bought|entry|entering"),
    (SELL, r"stc|sell\s+to\s+close|sell(?:ing)?|sold|clos(?:e|ed|ing)|exit(?:ed|ing)?|all\s+out|out\s+of"
           r"|stopped\s+out|stop\s+hit|cut(?:ting)?"),
]
# "In SPX 5800C 3.20" / "Out SPX" are common, but "in" and "out" are everyday words,
# so they only count as actions at the start of a line.
_LINE_START_PATTERNS: list[tuple[str, str]] = [
    (BUY, r"^\s*(?:i'?m\s+|i\s+am\s+|getting\s+)?in\b"),
    (SELL, r"^\s*(?:i'?m\s+|i\s+am\s+|getting\s+)?out\b"),
]

# Index options: an index cannot be bought as shares, and SPX weeklies/0DTE trade under the SPXW root.
INDEX_TICKERS = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "RUTW", "VIX", "VIXW", "DJX"}
DEFAULT_OPTION_ROOTS = {"SPX": "SPXW"}


@lru_cache(maxsize=32)
def _action_regex(extra: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> re.Pattern:
    words = list(_ACTION_PATTERNS)
    for action, phrases in extra:
        if phrases:
            words.append((action, "|".join(re.escape(ph).replace("\\ ", r"\s+") for ph in phrases)))
    parts = [f"(?P<{name}_{i}>\\b(?:{pat})\\b)" for i, (name, pat) in enumerate(words)]
    parts += [f"(?P<{name}_L{i}>{pat})" for i, (name, pat) in enumerate(_LINE_START_PATTERNS)]
    return re.compile("|".join(parts), re.IGNORECASE | re.MULTILINE)


# Stop-loss / target annotations carry numbers that must not be mistaken for the entry price.
_ANNOTATION_RE = re.compile(
    r"\b(?:sl|stop(?:\s*loss)?|stops?|pt|pts|tp|targets?|tgt)\b\s*[:@]?\s*\$?\d*\.?\d+"
    r"(?:\s*[,/]\s*\$?\d*\.?\d+)*",
    re.IGNORECASE,
)
_STRIKE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(c|p|calls?|puts?)\b", re.IGNORECASE)
_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ZERO_DTE_RE = re.compile(r"\b0\s*dte\b", re.IGNORECASE)
_AT_PRICE_RE = re.compile(r"(?:@|\bat\b)\s*\$?(\d*\.?\d+)", re.IGNORECASE)
_BARE_DECIMAL_RE = re.compile(r"(?<![\w/.])\$?(\d*\.\d+)(?![\w/.])")
_DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z]{1,6}(?:\.[A-Za-z])?)\b")
_WORD_RE = re.compile(r"\b[A-Za-z]{1,5}\b")

# Words that look like tickers but are not.
_STOPWORDS = {
    "A", "AN", "AND", "THE", "TO", "OF", "IN", "ON", "AT", "FOR", "OR", "MY", "ME", "I", "IM", "IS", "IT",
    "ALL", "OUT", "HALF", "SOME", "MORE", "HERE", "NOW", "THIS", "THAT", "WITH", "BACK", "JUST", "AGAIN",
    "BTO", "STC", "STO", "BTC", "BUY", "BUYING", "BOUGHT", "SELL", "SOLD", "LONG", "SHORT", "ENTRY",
    "ADD", "ADDED", "CLOSE", "EXIT", "TRIM", "OPEN", "SCALE", "PARTIAL", "TAKING", "PROFIT",
    "SL", "PT", "PTS", "TP", "TGT", "STOP", "LOSS", "TARGET", "C", "P", "CALL", "CALLS", "PUT", "PUTS",
    "DTE", "EXP", "AM", "PM", "ET", "EST", "PST", "CT", "LOTTO", "SWING", "DAY", "SCALP", "ALERT",
    "SIZE", "SMALL", "RISKY", "FILL", "FILLED", "AVG", "NEW", "POSITION", "SHARES", "CONTRACTS", "X",
    "GUYS", "GOOD", "TODAY", "WATCH", "LOOKS", "LIKE", "NICE", "GREAT", "NEXT", "WEEK", "BIG", "HUGE",
    "WIN", "WINS", "GAIN", "GAINS", "RUN", "ONE", "TWO", "TOO", "SO", "BE", "WE", "YOU", "IF", "BUT",
    "NOT", "NO", "YES", "UP", "DOWN", "OFF", "OVER", "FROM", "THEM", "THEY", "WILL", "CAN", "GET", "GOT",
    "HOLD", "STILL", "SOON", "EOD", "LETS", "VERY", "LOTS", "FEW", "RUNNER", "RUNNERS", "LEFT", "REST",
}


@dataclass
class Signal:
    action: str  # BUY, SELL or TRIM
    ticker: str
    asset_type: str  # STOCK or OPTION
    price: float | None = None
    strike: float | None = None
    right: str | None = None  # "C" or "P"
    expiration: date | None = None
    raw: str = field(default="", repr=False)

    @property
    def is_complete_contract(self) -> bool:
        return self.asset_type == OPTION and None not in (self.strike, self.right, self.expiration)

    @property
    def symbol(self) -> str:
        return self.broker_symbol()

    def broker_symbol(self, option_roots: dict[str, str] | None = None) -> str:
        """The ticker for stocks; the OCC symbol for a fully specified option (SPX maps to SPXW)."""
        if self.asset_type == OPTION:
            if not self.is_complete_contract:
                raise ValueError("option signal is missing strike, side or expiration")
            roots = DEFAULT_OPTION_ROOTS if option_roots is None else option_roots
            root = roots.get(self.ticker, self.ticker)
            return occ_symbol(root, self.expiration, self.right, self.strike)
        return self.ticker

    def describe(self) -> str:
        parts = [self.action.upper(), self.ticker]
        if self.asset_type == OPTION:
            if self.strike is not None:
                parts.append(f"{self.strike:g}{self.right or ''}")
            if self.expiration:
                parts.append(self.expiration.isoformat())
        if self.price is not None:
            parts.append(f"@ {self.price:g}")
        return " ".join(parts)


def occ_symbol(ticker: str, expiration: date, right: str, strike: float) -> str:
    """OCC option symbol as Alpaca expects it, e.g. SPY251020C00450000."""
    return f"{ticker.upper()}{expiration:%y%m%d}{right.upper()}{round(strike * 1000):08d}"


def _resolve_expiration(text: str, today: date) -> tuple[date | None, list[tuple[int, int]]]:
    if m := _ZERO_DTE_RE.search(text):
        return today, [m.span()]
    if m := _DATE_ISO_RE.search(text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), [m.span()]
        except ValueError:
            pass
    for m in _DATE_SLASH_RE.finditer(text):
        month, day, year = int(m[1]), int(m[2]), m[3]
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        try:
            if year:
                y = int(year)
                exp = date(y + 2000 if y < 100 else y, month, day)
            else:
                exp = date(today.year, month, day)
                if exp < today:  # "1/17" written in December means next January
                    exp = date(today.year + 1, month, day)
        except ValueError:
            continue
        return exp, [m.span()]
    return None, []


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _clean(text: str) -> str:
    # Drop Discord mentions, custom emoji and URLs so they don't produce false tickers.
    text = re.sub(r"<[@#:a-zA-Z0-9_!&]+>|https?://\S+|@everyone|@here", " ", text)
    return text.replace("*", " ").replace("_", " ").replace("`", " ")


def _extra_key(extra_words: dict[str, list[str]] | None) -> tuple:
    return tuple(sorted((k, tuple(v)) for k, v in (extra_words or {}).items()))


def detect_action(text: str, extra_words: dict[str, list[str]] | None = None) -> str | None:
    """Just the action (buy/sell/trim) in a message, e.g. "Sold" in a reply to an alert."""
    m = _action_regex(_extra_key(extra_words)).search(_clean(text))
    return m.lastgroup.split("_")[0] if m else None


def parse_signal(
    text: str,
    today: date | None = None,
    extra_words: dict[str, list[str]] | None = None,
    implicit_buy: bool = False,
) -> Signal | None:
    """Parse one message. Returns None when it is not a recognisable trade call-out.

    `extra_words` adds phrases per action, e.g. {"buy": ["loading"], "sell": ["cashed"]}.
    `implicit_buy` treats a full option call-out with a price and no action word
    ("QCOM 205C at 1.00 - lotto") as an entry.
    """
    today = today or date.today()
    raw = text
    text = _clean(text)

    action_match = _action_regex(_extra_key(extra_words)).search(text)
    if not action_match:
        if implicit_buy:
            signal = _parse_body(text, BUY, today, raw)
            if signal and signal.asset_type == OPTION and signal.price is not None:
                return signal
        return None
    action = action_match.lastgroup.split("_")[0]
    # Text before the action keyword is usually chatter ("ok guys"), so look after it first.
    # Some alerts put the action last ("SPX 5800C @ 3.20 BTO"); fall back to the whole message then.
    after = text[action_match.end():]
    whole = _blank(text, [action_match.span()])
    for body in (after, whole):
        if body.strip() and (signal := _parse_body(body, action, today, raw)):
            return signal
    return None


def _parse_body(body: str, action: str, today: date, raw: str) -> Signal | None:
    body = _ANNOTATION_RE.sub(" ", body)

    strike_m = _STRIKE_RE.search(body)
    expiration, date_spans = _resolve_expiration(body, today)
    is_option = strike_m is not None

    spans = list(date_spans)
    strike = right = None
    if strike_m:
        strike = float(strike_m[1])
        right = strike_m[2][0].upper()
        spans.append(strike_m.span())

    price = None
    if m := _AT_PRICE_RE.search(body):
        price = float(m[1])
        spans.append(m.span())
    else:
        remaining = _blank(body, spans)
        if m := _BARE_DECIMAL_RE.search(remaining):
            price = float(m[1])
            spans.append(m.span())

    ticker = None
    if m := _DOLLAR_TICKER_RE.search(body):
        ticker = m[1].upper()
    else:
        for w in _WORD_RE.finditer(_blank(body, spans)):
            candidate = w.group().upper()
            if candidate not in _STOPWORDS:
                ticker = candidate
                break
    if not ticker:
        return None

    return Signal(
        action=action,
        ticker=ticker,
        asset_type=OPTION if is_option else STOCK,
        price=price,
        strike=strike,
        right=right,
        expiration=expiration if is_option else None,
        raw=raw,
    )
