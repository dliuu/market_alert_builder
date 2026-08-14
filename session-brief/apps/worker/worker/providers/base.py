"""The vendor-agnostic contract. Every provider implements this so ingest and
normalize never learn a vendor's name — the whole point of putting the seam
here (docs/02-architecture.md: "you will switch, probably twice").

M2 uses only ``daily_bars``; ``quote``, ``earnings_calendar`` and ``news``
arrive with later milestones but are declared now to keep the seam stable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Protocol


class MarketDataProvider(Protocol):
    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        """Daily OHLCV records for [start, end], one dict per session in the
        vendor's native shape. Numeric fields are Decimal, never float."""
        ...

    def quote(self, symbol: str) -> dict[str, Any]:
        ...

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        ...

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        ...

    def latest_minute(self, symbol: str) -> dict[str, Any]:
        """Most recent minute bar's close as a synthetic session-close proxy
        (PM synthesis, M11). Returns ``{"symbol", "ts", "price": Decimal}``."""
        ...

    # --- Event calendars (open brief §4, M14) ------------------------------
    #
    # Declared, not implemented. The open brief seeds these synthetically (see
    # worker/events_seed.py) because the live source is Premium-tier and
    # redistribution needs a commercial plan (docs/02, D8) — the same licensing
    # gate M15 defers. The seam exists now so the swap is a provider call rather
    # than a reshape of §4 (D12: "you will switch, probably twice").

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """Upcoming ex-dividend dates across the symbol universe."""
        ...

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """Macro releases (CPI, FOMC, claims). These carry no symbol."""
        ...

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        ...


class CatalystProvider(Protocol):
    """Insider filings, Form 144s, and the reference data the catalyst
    detectors need (M17).

    A third protocol rather than more methods on ``MarketDataProvider``, for the
    reason M15 found the hard way (D28): protocols are structural, so widening
    the shared one makes ``TiingoProvider`` stop conforming to a contract it can
    never satisfy — there is no EOD-vendor call that returns a Form 4.

    ``offset`` exists because these endpoints take no date range and return
    newest-first, so bounded recency means walking pages from ``offset=0`` while
    records stay newer than the watermark. Never drain full history.
    """

    def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        """Form 4 transactions, newest first. Page size 50."""
        ...

    def proposed_sales(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        """Form 144 proposed sales, newest first. Page size 100."""
        ...

    def public_float(self, symbol: str) -> Decimal | None:
        """Shares in the public float, or None when the vendor doesn't say —
        which renders as "size unknown" rather than dropping the row
        (open question 4)."""
        ...


class PremarketProvider(Protocol):
    """Pre-market prints and the overnight macro tape.

    A separate protocol from MarketDataProvider because it is a separate,
    premium, licensing-gated feed (D8) — a vendor may serve either, both, or
    neither, and the EOD provider structurally cannot serve this.
    """

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Pre-market print and summed pre-market volume per symbol. Returns
        ``{"symbol", "extended_last": Decimal, "extended_v": int,
        "prev_close": Decimal}``; symbols with no prior close are omitted."""
        ...

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        """``{"symbol", "last": Decimal, "prev_close": Decimal}`` for futures."""
        ...

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Same shape, for index and yield series (VIX, 10Y)."""
        ...

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Same shape, for currency series (DXY)."""
        ...
