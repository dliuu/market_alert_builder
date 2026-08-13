"""fdnpy-backed provider (D21), added alongside Tiingo behind MarketDataProvider.

M11 needs only ``latest_minute()`` for PM synthesis. The spec named a
``get_latest_prices`` source that does not exist in this codebase, so the live
minute fetch is injected (``latest_minute_fn``) — fully testable offline — and
the real fdnpy premium call is wired once licensed (the M15 pattern).
``daily_bars`` / ``earnings_calendar`` / ``dividends`` are declared for M12 and
not implemented here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any


class FdnProvider:
    def __init__(
        self, api_key: str | None = None, *,
        latest_minute_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._key = api_key
        self._latest_minute_fn = latest_minute_fn

    def latest_minute(self, symbol: str) -> dict[str, Any]:
        if self._latest_minute_fn is not None:
            return self._latest_minute_fn(symbol)
        raise RuntimeError(
            "FdnProvider.latest_minute needs a live fdnpy feed (FDN_API_KEY) or an "
            "injected latest_minute_fn; no minute source is wired in M11"
        )

    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy daily_bars is wired when the premium feed lands")

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("earnings calendar feeds the open brief's §4 (M14/M15)")

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("dividends feed is an M12 concern")

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError

    # The §4 calendar seam (M14). fdnpy really does expose these —
    # get_earnings_calendar / get_dividends_calendar / get_economic_calendar —
    # but they are Premium tier, personal-use-only, with redistribution behind
    # Enterprise (docs/02, D8). That is the same licensing gate M15 defers, so
    # M14 seeds synthetically and these stay declared-but-unwired.

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy dividends calendar is Premium; M14 §4 seeds")

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy economic calendar is Premium; M14 §4 seeds")
