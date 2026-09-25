"""Option symbol and price-increment helpers shared by the trader and broker adapters."""

from __future__ import annotations

import math
import re
from datetime import date, datetime

from .parser import OPTION

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

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


def is_occ_symbol(symbol: str) -> bool:
    return bool(_OCC_RE.match(symbol))


def parse_occ(symbol: str) -> tuple[str, date, str, float]:
    """Split an OCC symbol into (root, expiration, "C"/"P", strike)."""
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol}")
    return m[1], datetime.strptime(m[2], "%y%m%d").date(), m[3], int(m[4]) / 1000
