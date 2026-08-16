"""The FinancialData.net feed. Two unrelated classes live here, and which one
you want depends on why you opened this file:

- ``FdnClient`` + ``FdnPremarketProvider`` (M16) are **the live feed**. They
  speak httpx directly to financialdata.net and are what the 08:00 open-brief
  ingest runs on whenever ``FDN_API_KEY`` is set. The other live fdn surfaces
  are ``worker/events_fdn.py`` (§4 calendars) and ``worker/news_fdn.py``
  (the §3 news gate) — both over the same ``FdnClient``.
- ``FdnProvider`` (M11) is the older ``MarketDataProvider``-shaped seam, kept
  for its injectable ``latest_minute()``. Its other methods are unimplemented
  stubs; read its class docstring before concluding anything about the feed
  from them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker import config
from worker.constants import FDN_TAPE_IDENTIFIERS

_FDN_BASE_URL = "https://financialdata.net/api/v1"

# Everything a bad vendor response can raise. HTTP failures, a body that isn't
# the list we expect (`FdnClient.fetch`'s own `ValueError`), malformed JSON
# (`json.JSONDecodeError` subclasses `ValueError`), an element that isn't a
# dict (`AttributeError`/`TypeError`/`KeyError` from treating it like one), or
# a non-numeric price/volume field such as a halted symbol's "N/A"
# (`decimal.InvalidOperation` subclasses `ArithmeticError`) — all of them
# degrade the section, never kill the job (the 08:15 run must survive a
# vendor having a bad morning). Every live-fetch catch site in the fdn feeds
# (`news_fdn`, `events_fdn`, `FdnPremarketProvider`) catches this tuple, not a
# narrower one.
#
# The cost: this is deliberately wider than "vendor shape errors" alone — a
# genuine bug in our own mapping code (a typo'd key, a `None` that slips into
# arithmetic) raises one of these same types and now degrades silently to
# "empty section" instead of failing loudly. We accept that trade because a
# quiet section beats a missing brief; the section-level non-degradation
# tests (e.g. `test_a_row_carries_dollars_and_a_volume_multiple`) are what
# would catch such a bug instead of a stack trace.
FEED_ERRORS = (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, ArithmeticError)

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
        self.captured.append((endpoint, _capture_key(params), response.text))
        return data

    def close(self) -> None:
        """Release the underlying connection pool. The worker is a long-lived
        process (docs/02) that builds a fresh client every 08:00 fire, so a
        client that is never closed leaks one pool per trading day."""
        self._client.close()


def _capture_key(params: dict[str, str]) -> str:
    """The `symbol` half of a raw_payloads row's identity.

    Symbol-scoped endpoints key on the identifier(s), as before. The
    symbol-less ones — the three calendars (`date=`) and news (`date=`,
    `offset=`) — used to collapse onto a single `'*'`, so eight days of
    `earnings-calendar` and three pages of news all landed on the same
    `(source, endpoint, symbol, as_of)` and `ON CONFLICT DO NOTHING` kept only
    the first. Invariant 5 says recomputation replays from `raw_payloads`, and
    it could not: §4 and the news gate were unreplayable. Folding the
    distinguishing params into the key lets the existing unique constraint
    separate them.

    `key` is the vendor secret. `FdnClient.fetch` adds it at request time, not
    through `params`, so it should never reach here — but it is excluded
    explicitly anyway, because the one thing this string must never do is put
    the API key in a database column (or in the probe's ✓ lines, which print
    the same values back to a human).

    Warning: the identifier short-circuit below ignores every other param. If
    `get_latest_prices` is ever paginated (the runbook's I3 remedy: page it
    with `offset`), those calls still pass `identifier` — every page would key
    on the same symbol and `ON CONFLICT DO NOTHING` would silently drop all
    but the first, exactly the collision this function was written to fix.
    The key must fold in the page/offset too, not just the identifier.
    """
    identifier = params.get("identifier") or params.get("identifiers")
    if identifier:
        return identifier
    distinguishing = sorted((k, v) for k, v in params.items() if k != "key")
    return "|".join(f"{k}={v}" for k, v in distinguishing) or "*"


_INSERT_RAW = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, covers_from, as_of, body)
    VALUES ('fdn', :endpoint, :symbol, :as_of, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, covers_from, as_of) DO NOTHING
""")


def store_captured_payloads(conn: Connection, client: FdnClient, *, as_of: date) -> int:
    """Invariant 5 for the fdn feeds: every captured response, verbatim.
    Batch endpoints store under the joined identifiers string; symbol-less
    endpoints (calendars, news) under their distinguishing params — see
    `_capture_key`, without which a whole window of calendar days would
    conflict onto one row and become unreplayable. Returns new rows written."""
    written = 0
    for endpoint, symbol, body in client.captured:
        result = conn.execute(
            _INSERT_RAW,
            {"endpoint": endpoint, "symbol": symbol, "as_of": as_of, "body": body},
        )
        written += result.rowcount
    return written


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
            except FEED_ERRORS:
                continue
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
        prior settle is the overnight read — no session-dated bar, no row.
        Deduped up front (order-preserving) so a repeated symbol costs one
        vendor call and yields one row, never two."""
        unique_symbols = list(dict.fromkeys(symbols))
        routed: dict[str, list[tuple[str, str]]] = {}
        for symbol in unique_symbols:
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
        return [by_symbol[s] for s in unique_symbols if s in by_symbol]

    def _futures_rows(self, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol, identifier in pairs:
            try:
                bars = self._client.fetch("futures-prices", identifier=identifier)
                bars.sort(key=lambda r: str(r["date"]), reverse=True)
                if len(bars) < 2 or str(bars[0]["date"]) != self._session.isoformat():
                    continue
                out.append({
                    "symbol": symbol,
                    "last": Decimal(str(bars[0]["close"])),
                    "prev_close": Decimal(str(bars[1]["close"])),
                })
            except FEED_ERRORS:
                continue
        return out

    def _quote_rows(self, endpoint: str, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        back = {identifier: symbol for symbol, identifier in pairs}
        try:
            records = self._client.fetch(
                endpoint, identifiers=",".join(identifier for _, identifier in pairs)
            )
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
        except FEED_ERRORS:
            return []


class FdnProvider:
    """The M11 ``MarketDataProvider``-shaped fdn seam. **Not the live feed.**

    Only ``latest_minute()`` is live, and only through an injected
    ``latest_minute_fn``. Every other method below is an unimplemented stub,
    and none of them is on the path the open brief actually runs: M16 wired
    the real feed elsewhere in this package, so a ``NotImplementedError`` here
    says nothing about whether that surface is live. The live homes are:

    - pre-market prints and the §2 tape → ``FdnPremarketProvider`` (this file)
    - §4 earnings / ex-div / macro calendars → ``worker/events_fdn.py``
    - the §3 ``has_news`` gate and narration headlines → ``worker/news_fdn.py``

    Kept rather than deleted because ``latest_minute`` is a real seam with a
    real caller; the stubs are the leftovers of a shape M16 did not need.
    """

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
        raise NotImplementedError("§4's live earnings calendar is worker/events_fdn.py (M16)")

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("dividends feed is an M12 concern")

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("live held-name news is worker/news_fdn.py (M16)")

    # The §4 calendar seam, declared in M14 and never wired *on this class*.
    # M16 shipped the live calendars over FdnClient in worker/events_fdn.py
    # instead — `date`-per-day fetches mapped onto the events-seed shape —
    # because §4 wants a session window, not a per-symbol range. These two
    # stubs are the unused older shape, not a statement that §4 is synthetic:
    # with FDN_API_KEY set, §4 is live.

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("live ex-div calendar is worker/events_fdn.py (M16)")

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("live macro calendar is worker/events_fdn.py (M16)")

    # The pre-market seam. `FdnPremarketProvider` — same file, a few hundred
    # lines up — implements these same four method names live against
    # FdnClient, and is what `ingest_premarket_for_session` constructs when
    # FDN_API_KEY is set. These four stubs are dead; call the provider.

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("live pre-market prints are FdnPremarketProvider (M16)")

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("live futures are FdnPremarketProvider (M16)")

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("live index quotes are FdnPremarketProvider (M16)")

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("live forex is FdnPremarketProvider (M16)")
