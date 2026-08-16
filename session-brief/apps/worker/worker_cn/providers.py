"""Deterministic synthetic EOD bars for A-share symbols (CN-M1).

Same determinism idiom as ``worker/providers/synthetic.py``: every value is a
pure function of ``(symbol, date, axis)`` through a sha256 hash, never
``random`` and never a cumulative walk over the requested window — two
different backfill windows must produce byte-identical bars for the same day.
Stands in for the live CN vendor until CN-M3
(``worker_cn.config.cn_bars_are_synthetic``).

Only ``daily_bars`` is implemented, mirroring ``TiingoProvider``
(``worker/providers/tiingo.py``) — the other ``MarketDataProvider`` methods
are declared to satisfy the protocol but raise.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from worker.providers.base import MarketDataProvider
from worker.providers.tiingo import TiingoProvider
from worker_cn.calendar import CN
from worker_cn.config import cn_bars_are_synthetic

_CENT = Decimal("0.01")

# Per-symbol base level, scaled to a plausible CNY price.
_BASE_LOW = Decimal("10")
_BASE_SPAN = Decimal("1990")  # base lands in [10, 2000)

# Day-to-day close spread around the symbol's base level.
_CLOSE_SPREAD = Decimal("0.04")
# Open's spread around the day's close.
_OPEN_SPREAD = Decimal("0.02")
# High/low wick room beyond the o/c bracket.
_WICK_SPAN = Decimal("0.02")

_VOL_BASE = 1_000_000


class SyntheticCnBarsProvider:
    """Deterministic ``MarketDataProvider`` for XSHG-session A-share bars."""

    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        d = start
        while d <= end:
            if CN.is_session(d):
                records.append(self._bar(symbol, d))
            d += timedelta(days=1)
        return records

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def latest_minute(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("SyntheticCnBarsProvider only implements daily_bars")

    # --- internals -----------------------------------------------------

    def _bar(self, symbol: str, d: date) -> dict[str, Any]:
        base = _BASE_LOW + self._unit_symbol(symbol, "base") * _BASE_SPAN
        close = self._round(
            base * (1 + _CLOSE_SPREAD * (self._unit_day(symbol, d, "close") - Decimal("0.5")))
        )
        open_ = self._round(
            close * (1 + _OPEN_SPREAD * (self._unit_day(symbol, d, "open") - Decimal("0.5")))
        )
        hi_base = max(open_, close)
        lo_base = min(open_, close)
        high = self._round(hi_base * (1 + self._unit_day(symbol, d, "high") * _WICK_SPAN))
        low = self._round(lo_base * (1 - self._unit_day(symbol, d, "low") * _WICK_SPAN))
        volume = _VOL_BASE + int(self._unit_day(symbol, d, "volume") * _VOL_BASE * 4)
        return {
            "date": f"{d.isoformat()}T00:00:00.000Z",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "adjClose": close,
        }

    @staticmethod
    def _round(value: Decimal) -> Decimal:
        return value.quantize(_CENT)

    def _unit_symbol(self, symbol: str, salt: str) -> Decimal:
        """A stable value in [0, 1) for this symbol, this axis — never the day,
        so the per-symbol base level is window-independent by construction."""
        digest = hashlib.sha256(f"{symbol}|{salt}".encode()).digest()
        return Decimal(int.from_bytes(digest[:4], "big")) / Decimal(1 << 32)

    def _unit_day(self, symbol: str, d: date, salt: str) -> Decimal:
        """A stable value in [0, 1) for this symbol, this day, this axis."""
        digest = hashlib.sha256(f"{symbol}|{d.isoformat()}|{salt}".encode()).digest()
        return Decimal(int.from_bytes(digest[:4], "big")) / Decimal(1 << 32)


def default_cn_bars_provider() -> MarketDataProvider:
    """The CN bars provider seam (CN-M3, Task 10): `SyntheticCnBarsProvider`
    while `cn_bars_are_synthetic()` is True, else the live `TiingoProvider`.
    Both `worker_cn/scheduler.py`'s `_default_cn_provider()` and `backfill
    --market cn` (`worker_cn/backfill.py`) route through this single seam, so
    the switch-on lives in exactly one place."""
    if cn_bars_are_synthetic():
        return SyntheticCnBarsProvider()
    return TiingoProvider()
