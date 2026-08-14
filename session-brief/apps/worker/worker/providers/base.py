"""The vendor-agnostic contract. Every provider implements this so ingest and
normalize never learn a vendor's name — the whole point of putting the seam
here (docs/02-architecture.md: "you will switch, probably twice").

M2 uses only ``daily_bars``; ``quote``, ``earnings_calendar`` and ``news``
arrive with later milestones but are declared now to keep the seam stable.
"""

from __future__ import annotations

from datetime import date
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
