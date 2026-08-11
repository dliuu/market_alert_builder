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
