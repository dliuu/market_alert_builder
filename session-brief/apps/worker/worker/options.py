"""Vendor-neutral option types and the OCC symbol parser.

Kept apart from ``providers/cboe.py`` so the volatility maths and the stored
shapes never import a vendor: a second options feed would parse into these
same dataclasses. Units are fixed here, not at the call site: ``iv`` and
``iv30`` are in vol points (26.8, not 0.268), prices are ``Decimal``, and
``timestamp`` is timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class OccContract:
    root: str
    expiry: date
    right: str  # "C" | "P"
    strike: Decimal


def parse_occ(symbol: str) -> OccContract:
    """``AAPL261121C00340000`` → root ``AAPL``, expiry 2026-11-21, call,
    strike 340. Parsed from the right because roots run one to six characters:
    eight strike digits (three implied decimals), the right, six date digits."""
    if len(symbol) < 16 or symbol[-9] not in "CP" or not symbol[-8:].isdigit():
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    root = symbol[:-15]
    if not root or not symbol[-15:-9].isdigit():
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    expiry = datetime.strptime(symbol[-15:-9], "%y%m%d").date()
    strike = Decimal(symbol[-8:]) / Decimal(1000)
    return OccContract(root=root, expiry=expiry, right=symbol[-9], strike=strike)


@dataclass(frozen=True)
class Underlying:
    symbol: str
    close: Decimal
    current_price: Decimal
    prev_day_close: Decimal | None
    iv30: Decimal | None  # the vendor's own, vol points
    timestamp: datetime  # UTC


@dataclass(frozen=True)
class Contract:
    symbol: str
    expiry: date
    right: str
    strike: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    iv: Decimal | None  # vol points; None where the vendor emitted 0
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None
    open_interest: int
    volume: int
    last: Decimal | None
    prev_day_close: Decimal | None
    percent_change: Decimal | None  # the vendor's own, last-trade based

    @property
    def two_sided(self) -> bool:
        return self.bid > 0 and self.ask > self.bid

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class IndexBar:
    session_date: date
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741 — matches the OHLC column name
    c: Decimal
