"""Tiingo daily EOD provider (D12). Free tier: personal, non-commercial.

Only ``daily_bars`` is implemented — Tiingo's free tier covers neither an
earnings calendar nor news, which are TBD source gaps for M7/M8.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from worker.config import TIINGO_API_KEY

_BASE_URL = "https://api.tiingo.com/tiingo/daily"

# CN ticker formats (CN-Q1, cn/docs/open-questions.md), keyed by the internal
# symbol suffix. Suffix -> a format template taking the numeric `code`.
# `tiingo-cn-probe` (2026-08-16 live run) confirmed the bare numeric code
# resolves for both exchanges against Tiingo's free tier — the originally
# guessed `-SHG`/`-SHE` dash format, the `.SS`/`.SZ` passthrough, and the
# `-SS`/`-SZ` dash format all 404. Update these values if a later probe run
# reports a different working format (e.g. a Tiingo API change).
CN_TIINGO_FORMATS: dict[str, str] = {
    ".SS": "{code}",
    ".SZ": "{code}",
}


class TiingoProvider:
    def __init__(self, api_key: str | None = None, *, base_url: str = _BASE_URL) -> None:
        key = api_key if api_key is not None else TIINGO_API_KEY
        if not key:
            raise RuntimeError("TIINGO_API_KEY is not set (see repo-root .env)")
        self._key = key
        self._base_url = base_url

    def _vendor_symbol(self, symbol: str) -> str:
        """Maps an internal symbol to the ticker string Tiingo expects. US
        symbols pass through unchanged (lowercased at the request site, as
        today). CN symbols (`*.SS`/`*.SZ`) map via `CN_TIINGO_FORMATS`."""
        symbol_upper = symbol.upper()
        for suffix, fmt in CN_TIINGO_FORMATS.items():
            if symbol_upper.endswith(suffix):
                code = symbol[: -len(suffix)]
                return fmt.format(code=code)
        return symbol

    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        return self._fetch_daily_bars(self._vendor_symbol(symbol), start, end)

    def _fetch_daily_bars(
        self, vendor_symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """The actual request, against an already-vendor-formatted symbol —
        split out from `daily_bars` so `tiingo-cn-probe` can try several
        candidate vendor symbols for the same internal symbol without going
        through `_vendor_symbol`'s single guess."""
        # Key travels in the Authorization header, never the URL (no secrets in
        # query strings). parse_float=Decimal keeps prices off the float path.
        response = httpx.get(
            f"{self._base_url}/{vendor_symbol.lower()}/prices",
            params={"startDate": start.isoformat(), "endDate": end.isoformat()},
            headers={"Authorization": f"Token {self._key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        data = json.loads(response.text, parse_float=Decimal)
        if not isinstance(data, list):
            raise ValueError(f"Tiingo returned non-list for {vendor_symbol}: {data!r}")
        return data

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("Tiingo quote endpoint is not wired until it's needed")

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("earnings calendar is a TBD source gap (M7)")

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("company news is a TBD source gap (M8)")

    def latest_minute(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError(
            "Tiingo free tier has no minute data; fdn's latest-prices is the live "
            "minute source (FdnPremarketProvider, M16)"
        )

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("dividends feed is an M12 concern")

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Tiingo free tier has no dividends calendar; §4 uses worker/events_fdn.py "
            "live (M16) and events_seed.py keyless"
        )

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Tiingo has no economic calendar; §4 uses worker/events_fdn.py live (M16) "
            "and events_seed.py keyless"
        )
