"""fdnpy-backed provider (D21), added alongside Tiingo behind MarketDataProvider.

M11 needs only ``latest_minute()`` for PM synthesis. The spec named a
``get_latest_prices`` source that does not exist in this codebase, so the live
minute fetch is injected (``latest_minute_fn``) — fully testable offline — and
the real fdnpy premium call is wired once licensed (the M15 pattern).
``daily_bars`` / ``earnings_calendar`` / ``dividends`` are declared for M12 and
not implemented here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from worker import config

_FDN_BASE_URL = "https://financialdata.net/api/v1"


class FdnClient:
    """Thin transport over financialdata.net (M16). Deliberately not the fdnpy
    SDK: fdnpy parses prices as float, and the money invariant wants
    parse_float=Decimal on every byte — the same reason TiingoProvider speaks
    httpx directly. The vendor authenticates via a `key` query parameter (it
    has no header auth); never log request URLs.

    Every successful fetch is captured as (endpoint, symbol, verbatim text) so
    the caller can honour invariant 5 (raw payloads stored verbatim) — the
    client itself never touches the database.
    """

    def __init__(
        self, api_key: str | None = None, *,
        base_url: str = _FDN_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key if api_key is not None else config.FDN_API_KEY
        if not key:
            raise RuntimeError("FDN_API_KEY is not set (see repo-root .env)")
        self._key = key
        self._base_url = base_url
        self._client = httpx.Client(transport=transport, timeout=30.0)
        self.captured: list[tuple[str, str, str]] = []

    def fetch(self, endpoint: str, **params: str) -> list[dict[str, Any]]:
        response = self._client.get(
            f"{self._base_url}/{endpoint}", params={**params, "key": self._key}
        )
        response.raise_for_status()
        data = json.loads(response.text, parse_float=Decimal)
        if not isinstance(data, list):
            raise ValueError(f"fdn returned non-list for {endpoint}: {data!r}")
        symbol = params.get("identifier") or params.get("identifiers") or "*"
        self.captured.append((endpoint, symbol, response.text))
        return data


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

    # The M15 pre-market seam. fdnpy exposes all four (get_latest_prices,
    # get_futures_prices, get_index_quotes, get_forex_quotes) at Premium tier,
    # personal-use-only, with redistribution behind Enterprise (docs/02, D8).
    # Until that is licensed the open brief runs on SyntheticPremarketProvider —
    # a business decision, not a code blocker.

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy latest prices are Premium; M15 seeds synthetically")

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy futures are Premium; M15 seeds synthetically")

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy index quotes are Premium; M15 seeds synthetically")

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy forex is Premium; M15 seeds synthetically")
