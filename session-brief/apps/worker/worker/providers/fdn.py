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
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

import httpx

from worker import config
from worker.constants import FDN_TAPE_IDENTIFIERS

_FDN_BASE_URL = "https://financialdata.net/api/v1"

_PREMARKET_OPEN_ET = clock_time(4, 0)  # extended-hours open; window end is capture_stamp


def _parse_fdn_time(value: str) -> datetime:
    """fdn minute timestamps ("YYYY-MM-DD HH:MM:SS") carry no zone; observed
    values are UTC (spec: verified against the documented MSFT example; the
    fdn-probe CLI re-verifies live the day the key lands)."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


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


class FdnPremarketProvider:
    """Live PremarketProvider over FdnClient (M16). Constructor mirrors
    SyntheticPremarketProvider — prior closes come from the caller, a provider
    does not touch the database. A symbol with no prior close, no vendor
    identifier, no window prints, or a failed feed is omitted, never invented.
    Feed failures are per-symbol/per-endpoint and non-fatal: an empty feed
    renders the section's omitted-note (M14), it never kills the 08:15 job.
    """

    def __init__(
        self, client: FdnClient, prior_closes: dict[str, Decimal], session_date: date
    ) -> None:
        from worker import calendar
        from worker.premarket import capture_stamp

        self._client = client
        self._closes = prior_closes
        self._session = session_date
        self._window_start = datetime.combine(
            session_date, _PREMARKET_OPEN_ET, tzinfo=calendar.ET
        ).astimezone(UTC)
        self._window_end = capture_stamp(session_date)

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            prev = self._closes.get(symbol)
            if prev is None:
                continue
            try:
                records = self._client.fetch("latest-prices", identifier=symbol)
            except httpx.HTTPError:
                continue
            window = [
                r for r in records
                if self._window_start <= _parse_fdn_time(str(r["time"])) <= self._window_end
            ]
            if not window:
                continue
            window.sort(key=lambda r: str(r["time"]))
            out.append({
                "symbol": symbol,
                "extended_last": Decimal(str(window[-1]["close"])),
                "extended_v": int(sum(Decimal(str(r.get("volume") or 0)) for r in window)),
                "prev_close": prev,
            })
        return out

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    # --- internals ---------------------------------------------------------

    def _tape(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Route each internal symbol through FDN_TAPE_IDENTIFIERS. Quote
        endpoints are batched (identifiers=a,b); futures are one call per
        identifier with daily bars, where the session-dated bar against the
        prior settle is the overnight read — no session-dated bar, no row."""
        routed: dict[str, list[tuple[str, str]]] = {}
        for symbol in symbols:
            route = FDN_TAPE_IDENTIFIERS.get(symbol)
            if route is not None:
                routed.setdefault(route[0], []).append((symbol, route[1]))

        out: list[dict[str, Any]] = []
        for endpoint, pairs in routed.items():
            if endpoint == "futures-prices":
                out.extend(self._futures_rows(pairs))
            else:
                out.extend(self._quote_rows(endpoint, pairs))
        by_symbol = {row["symbol"]: row for row in out}
        return [by_symbol[s] for s in symbols if s in by_symbol]

    def _futures_rows(self, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol, identifier in pairs:
            try:
                bars = self._client.fetch("futures-prices", identifier=identifier)
            except httpx.HTTPError:
                continue
            bars.sort(key=lambda r: str(r["date"]), reverse=True)
            if len(bars) < 2 or str(bars[0]["date"]) != self._session.isoformat():
                continue
            out.append({
                "symbol": symbol,
                "last": Decimal(str(bars[0]["close"])),
                "prev_close": Decimal(str(bars[1]["close"])),
            })
        return out

    def _quote_rows(self, endpoint: str, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        back = {identifier: symbol for symbol, identifier in pairs}
        try:
            records = self._client.fetch(
                endpoint, identifiers=",".join(identifier for _, identifier in pairs)
            )
        except httpx.HTTPError:
            return []
        out: list[dict[str, Any]] = []
        for record in records:
            symbol = back.get(str(record.get("trading_symbol")))
            if symbol is None or record.get("price") is None or record.get("change") is None:
                continue
            last = Decimal(str(record["price"]))
            out.append({
                "symbol": symbol,
                "last": last,
                "prev_close": last - Decimal(str(record["change"])),
            })
        return out


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
