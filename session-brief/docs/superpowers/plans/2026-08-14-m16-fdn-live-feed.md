# M16 — FdnProvider Live Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the open brief's synthetic pre-market feed, §4 calendar seed, and dead news gate to live FinancialData.net data behind the existing `PremarketProvider` seam, switchable by the presence of `FDN_API_KEY` and fully testable offline.

**Architecture:** One new transport class (`FdnClient`, direct httpx with `parse_float=Decimal`, no fdnpy dependency), one adapter implementing the four `PremarketProvider` methods (`FdnPremarketProvider`), two small fetch-and-map modules for calendars and news, and a live/synthetic branch at the scheduler's existing construction points. No BriefObject shape change; no `schema_version` bump; no Alembic migration.

**Tech Stack:** Python 3.12, httpx (+ `httpx.MockTransport` in tests), SQLAlchemy Core, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-m16-fdn-live-feed-design.md` — read it first; it carries the verified vendor response shapes and the eight design decisions this plan implements.

## Global Constraints

- `ruff` and `mypy --strict` clean; run `uv run ruff check worker tests && uv run mypy worker` before every commit (all commands from `apps/worker/`).
- Money and prices are `Decimal`, never float: every fdn JSON parse uses `json.loads(response.text, parse_float=Decimal)`.
- UTC at rest, `America/New_York` in logic (invariant 8).
- A provider never touches the database (docstring rule in `synthetic.py`).
- A symbol with no prior close, no vendor identifier, or a failed feed is **omitted, never invented** (M15 standing rule).
- Raw vendor payloads are stored verbatim in `raw_payloads` (invariant 5).
- Live-feed failures degrade (empty feed → omitted-note section); the 08:15 job must not crash on a vendor 5xx.
- `FDN_API_KEY` unset ⇒ behavior is bit-for-bit today's synthetic path; the existing test suite must pass unmodified except where a task says otherwise.
- The vendor authenticates via a `key` **query parameter** (no header auth exists). Accepted deviation from the Tiingo header rule; never log request URLs.
- DB-touching tests use the existing `db_conn` fixture from `tests/conftest.py` and `Z`-prefixed fake symbols (see `test_events_seed.py`).

---

### Task 1: Config — `FDN_API_KEY` and the derived synthetic flag

**Files:**
- Modify: `worker/config.py` (append after the `PREMARKET_VOL_*` block, line 76)
- Modify: `worker/constants.py:81-91` (delete `PREMARKET_FEED_IS_SYNTHETIC` and its comment block)
- Modify: `worker/assemble_open.py:48,146` (read config instead of the constant)
- Modify: `worker/scheduler.py:354` (docstring reference)
- Test: `tests/test_config_fdn.py` (create), `tests/test_assemble_open.py` (existing suite must stay green)

**Interfaces:**
- Consumes: nothing new.
- Produces: `config.FDN_API_KEY: str` and `config.premarket_feed_is_synthetic() -> bool` — every later task branches on these two names.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_fdn.py
from __future__ import annotations

import pytest

from worker import config


def test_synthetic_flag_derives_from_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "FDN_API_KEY", "")
    assert config.premarket_feed_is_synthetic() is True
    monkeypatch.setattr(config, "FDN_API_KEY", "fdn_test_key")
    assert config.premarket_feed_is_synthetic() is False
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_config_fdn.py -v`
Expected: FAIL — `AttributeError: module 'worker.config' has no attribute 'FDN_API_KEY'`

- [ ] **Step 3: Implement**

Append to `worker/config.py`:

```python
# FinancialData.net (M16). One key is also the live/synthetic switch: empty ⇒
# the open brief's §2/§3/§4 run on the deterministic synthetic feed exactly as
# M14/M15 shipped them; set ⇒ FdnClient serves live pre-market, calendar, and
# news data. Premium tier ($69/mo, personal use) covers every endpoint we call.
FDN_API_KEY: str = os.environ.get("FDN_API_KEY", "")


def premarket_feed_is_synthetic() -> bool:
    """Whether §2/§3 are running on invented levels. Derived from the key, never
    hand-flipped — the constants.py flag this replaces (M15) rotted the moment
    it and the provider construction could disagree. `assemble_open` reads this
    to stamp `overnight_tape.synthetic` into `data_quality.stale`, which is the
    single source both renderers key their banner off."""
    return not FDN_API_KEY
```

In `worker/constants.py`, delete lines 81–91 (the `PREMARKET_FEED_IS_SYNTHETIC` comment block and constant). In `worker/assemble_open.py` change line 48 from
`from worker.constants import PREMARKET_FEED_IS_SYNTHETIC` to
`from worker import config`, and line 146 from
`if tape_section["rows"] and PREMARKET_FEED_IS_SYNTHETIC:` to
`if tape_section["rows"] and config.premarket_feed_is_synthetic():`.
In `worker/scheduler.py:354` update the docstring's
`` `constants.PREMARKET_FEED_IS_SYNTHETIC` `` reference to
`` `config.premarket_feed_is_synthetic()` ``.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_fdn.py tests/test_assemble_open.py tests/test_assemble_open_db.py -v`
Expected: PASS (the fixture snapshots still see the synthetic stale entry because `FDN_API_KEY` is unset in tests).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/config.py worker/constants.py worker/assemble_open.py worker/scheduler.py tests/test_config_fdn.py
git commit -m "feat(m16): FDN_API_KEY config; synthetic flag derived from key presence"
```

---

### Task 2: `FdnClient` — the transport

**Files:**
- Modify: `worker/providers/fdn.py` (add `FdnClient` above `FdnProvider`)
- Test: `tests/test_fdn.py` (append)

**Interfaces:**
- Consumes: `config.FDN_API_KEY` (Task 1).
- Produces: `FdnClient(api_key: str | None = None, *, base_url: str = _FDN_BASE_URL, transport: httpx.BaseTransport | None = None)` with
  `fetch(endpoint: str, **params: str) -> list[dict[str, Any]]` and
  `captured: list[tuple[str, str, str]]` — `(endpoint, symbol_or_star, response_text)`, appended per successful fetch. Tasks 3–7 call `fetch`; Task 5 drains `captured`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_fdn.py
import httpx

from worker.providers.fdn import FdnClient


def _client(handler: object) -> FdnClient:
    return FdnClient("k", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_fetch_parses_prices_as_decimal_and_sends_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "k"
        assert request.url.path == "/api/v1/latest-prices"
        assert request.url.params["identifier"] == "ASTS"
        return httpx.Response(200, text='[{"trading_symbol": "ASTS", "close": 74.31}]')

    got = _client(handler).fetch("latest-prices", identifier="ASTS")
    assert got == [{"trading_symbol": "ASTS", "close": Decimal("74.31")}]
    assert not isinstance(got[0]["close"], float)


def test_fetch_captures_the_verbatim_response_for_raw_payloads() -> None:
    body = '[{"trading_symbol": "ES", "close": 5620.0}]'
    client = _client(lambda _req: httpx.Response(200, text=body))
    client.fetch("futures-prices", identifier="ES")
    assert client.captured == [("futures-prices", "ES", body)]


def test_fetch_refuses_a_non_list_body() -> None:
    client = _client(lambda _req: httpx.Response(200, text='{"error": "nope"}'))
    with pytest.raises(ValueError):
        client.fetch("latest-prices", identifier="ASTS")


def test_client_without_a_key_refuses() -> None:
    with pytest.raises(RuntimeError):
        FdnClient("")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_fdn.py -v`
Expected: FAIL — `ImportError: cannot import name 'FdnClient'`

- [ ] **Step 3: Implement**

Add to `worker/providers/fdn.py` (new imports: `json`, `httpx`, `Decimal`, `worker.config`):

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fdn.py -v` — Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/providers/fdn.py tests/test_fdn.py
git commit -m "feat(m16): FdnClient transport — Decimal parsing, response capture"
```

---

### Task 3: `FdnPremarketProvider` — held names (§3, `get_latest_prices`)

**Files:**
- Modify: `worker/providers/fdn.py` (add `FdnPremarketProvider`)
- Test: `tests/test_fdn_premarket.py` (create)

**Interfaces:**
- Consumes: `FdnClient.fetch` (Task 2); `premarket.capture_stamp(session_date)` (existing); `calendar.ET` (existing).
- Produces: `FdnPremarketProvider(client: FdnClient, prior_closes: dict[str, Decimal], session_date: date)` implementing all four `PremarketProvider` methods. This task implements `get_latest_prices(symbols: list[str]) -> list[dict[str, Any]]` returning `{"symbol", "extended_last": Decimal, "extended_v": int, "prev_close": Decimal}`; Task 4 fills the three tape methods (stub them `return []` for now so the class satisfies the protocol).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fdn_premarket.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from worker.providers.fdn import FdnClient, FdnPremarketProvider

_SESSION = date(2026, 8, 14)  # EDT: pre-market window is 08:00–12:12 UTC


def _provider(handler: object, closes: dict[str, Decimal]) -> FdnPremarketProvider:
    client = FdnClient("k", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return FdnPremarketProvider(client, closes, _SESSION)


def _minute(time: str, close: str, volume: str) -> str:
    return (f'{{"trading_symbol": "ASTS", "time": "{time}", "open": {close}, '
            f'"high": {close}, "low": {close}, "close": {close}, "volume": {volume}}}')


def test_latest_prices_filters_to_the_premarket_window_and_sums_volume() -> None:
    body = "[" + ",".join([
        _minute("2026-08-13 19:59:00", "70.00", "9000"),   # yesterday: out
        _minute("2026-08-14 07:59:00", "71.00", "500"),    # 03:59 ET: out
        _minute("2026-08-14 08:01:00", "74.10", "1200"),   # 04:01 ET: in
        _minute("2026-08-14 12:10:00", "74.55", "800"),    # 08:10 ET: in, last
        _minute("2026-08-14 12:30:00", "75.00", "600"),    # after capture: out
    ]) + "]"
    p = _provider(lambda _r: httpx.Response(200, text=body), {"ASTS": Decimal("74.31")})
    got = p.get_latest_prices(["ASTS"])
    assert got == [{
        "symbol": "ASTS",
        "extended_last": Decimal("74.55"),
        "extended_v": 2000,
        "prev_close": Decimal("74.31"),
    }]


def test_latest_prices_omits_a_name_with_no_prior_close() -> None:
    p = _provider(lambda _r: httpx.Response(200, text="[]"), {})
    assert p.get_latest_prices(["ASTS"]) == []


def test_latest_prices_omits_a_name_with_no_window_prints() -> None:
    body = "[" + _minute("2026-08-13 19:59:00", "70.00", "9000") + "]"
    p = _provider(lambda _r: httpx.Response(200, text=body), {"ASTS": Decimal("74.31")})
    assert p.get_latest_prices(["ASTS"]) == []


def test_latest_prices_survives_a_vendor_500_by_omitting() -> None:
    p = _provider(lambda _r: httpx.Response(500), {"ASTS": Decimal("74.31")})
    assert p.get_latest_prices(["ASTS"]) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_fdn_premarket.py -v`
Expected: FAIL — `ImportError: cannot import name 'FdnPremarketProvider'`

- [ ] **Step 3: Implement**

Add to `worker/providers/fdn.py` (new imports: `datetime`, `time as clock_time`, `UTC` via `zoneinfo.ZoneInfo("UTC")` — match `scheduler.py`'s pattern):

```python
_PREMARKET_OPEN_ET = clock_time(4, 0)  # extended-hours open; window end is capture_stamp


def _parse_fdn_time(value: str) -> datetime:
    """fdn minute timestamps ("YYYY-MM-DD HH:MM:SS") carry no zone; observed
    values are UTC (spec: verified against the documented MSFT example; the
    fdn-probe CLI re-verifies live the day the key lands)."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


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
        return []  # Task 4

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return []  # Task 4

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return []  # Task 4
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fdn_premarket.py -v` — Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/providers/fdn.py tests/test_fdn_premarket.py
git commit -m "feat(m16): FdnPremarketProvider held names — minute-window filter, summed volume"
```

---

### Task 4: `FdnPremarketProvider` — the tape (§2)

**Files:**
- Modify: `worker/constants.py` (add `FDN_TAPE_IDENTIFIERS` where `TAPE_SEED_LEVELS`' comment sits)
- Modify: `worker/providers/fdn.py` (replace the three Task-3 stubs)
- Test: `tests/test_fdn_premarket.py` (append)

**Interfaces:**
- Consumes: `FdnClient.fetch` (Task 2), `FdnPremarketProvider` (Task 3).
- Produces: `constants.FDN_TAPE_IDENTIFIERS: dict[str, tuple[str, str]]` (internal symbol → `(endpoint, fdn identifier)`), and the three tape methods each returning `{"symbol", "last": Decimal, "prev_close": Decimal}` keyed by **internal** symbol.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_fdn_premarket.py

def test_index_quotes_derive_prev_close_from_price_minus_change() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/index-quotes"
        assert request.url.params["identifiers"] == "^TNX,^VIX"
        return httpx.Response(200, text=(
            '[{"trading_symbol": "^TNX", "price": 4.25, "change": 0.03},'
            ' {"trading_symbol": "^VIX", "price": 15.50, "change": -0.75}]'
        ))

    got = _provider(handler, {}).get_index_quotes(["^TNX", "^VIX"])
    assert got == [
        {"symbol": "^TNX", "last": Decimal("4.25"), "prev_close": Decimal("4.22")},
        {"symbol": "^VIX", "last": Decimal("15.50"), "prev_close": Decimal("16.25")},
    ]


def test_forex_route_maps_dxy_to_the_index_endpoint_and_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/index-quotes"
        assert request.url.params["identifiers"] == "^DXY"
        return httpx.Response(
            200, text='[{"trading_symbol": "^DXY", "price": 103.40, "change": 0.20}]'
        )

    got = _provider(handler, {}).get_forex_quotes(["DXY"])
    assert got == [
        {"symbol": "DXY", "last": Decimal("103.40"), "prev_close": Decimal("103.20")}
    ]


def test_futures_use_the_session_dated_bar_over_the_prior_settle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/futures-prices"
        assert request.url.params["identifier"] == "ES"
        return httpx.Response(200, text=(
            '[{"trading_symbol": "ES", "date": "2026-08-13", "close": 5600.00},'
            ' {"trading_symbol": "ES", "date": "2026-08-14", "close": 5620.00}]'
        ))

    got = _provider(handler, {}).get_futures_prices(["ES=F"])
    assert got == [
        {"symbol": "ES=F", "last": Decimal("5620.00"), "prev_close": Decimal("5600.00")}
    ]


def test_futures_with_no_session_dated_bar_are_omitted() -> None:
    body = '[{"trading_symbol": "ES", "date": "2026-08-13", "close": 5600.00}]'
    got = _provider(lambda _r: httpx.Response(200, text=body), {}).get_futures_prices(["ES=F"])
    assert got == []


def test_an_unmapped_tape_symbol_is_omitted_without_a_fetch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch for an unmapped symbol")

    assert _provider(handler, {}).get_index_quotes(["^UNMAPPED"]) == []


def test_a_tape_endpoint_500_yields_an_empty_feed() -> None:
    got = _provider(lambda _r: httpx.Response(500), {}).get_index_quotes(["^TNX"])
    assert got == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_fdn_premarket.py -v`
Expected: the six new tests FAIL (stubs return `[]`; the two omission tests pass vacuously — confirm the other four fail).

- [ ] **Step 3: Implement**

In `worker/constants.py`, add below `FOREIGN_PROXIES`:

```python
# fdn identifier routing for the tape universe (M16). Internal symbol →
# (fdn endpoint, fdn identifier). The routing table, not the seam method, picks
# the endpoint: `tape_universe` tags the foreign-proxy ETFs "index", but on fdn
# they are stocks, so they route to stock-quotes. A symbol absent here is
# omitted, never invented. Futures/caret identifiers are the documented best
# guess ("ZN"-style) — `fdn-probe` verifies each one live once the key lands.
FDN_TAPE_IDENTIFIERS: dict[str, tuple[str, str]] = {
    "ES=F": ("futures-prices", "ES"),
    "NQ=F": ("futures-prices", "NQ"),
    "CL=F": ("futures-prices", "CL"),
    "^TNX": ("index-quotes", "^TNX"),
    "^VIX": ("index-quotes", "^VIX"),
    "DXY": ("index-quotes", "^DXY"),
    "EWT": ("stock-quotes", "EWT"),
    "EWJ": ("stock-quotes", "EWJ"),
    "EWC": ("stock-quotes", "EWC"),
    "EUFN": ("stock-quotes", "EUFN"),
    "EWG": ("stock-quotes", "EWG"),
}
```

In `worker/providers/fdn.py`, replace the three stubs (import `FDN_TAPE_IDENTIFIERS` from `worker.constants`):

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fdn_premarket.py -v` — Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/constants.py worker/providers/fdn.py tests/test_fdn_premarket.py
git commit -m "feat(m16): FdnPremarketProvider tape — identifier routing, quotes and futures adapters"
```

---

### Task 5: Scheduler swap point + raw-payload capture

**Files:**
- Modify: `worker/scheduler.py:340-416` (`ingest_premarket_for_session`)
- Modify: `worker/providers/fdn.py` (add module-level `store_captured_payloads`)
- Test: `tests/test_scheduler_fdn.py` (create; needs `db_conn`-style engine — reuse the pattern from `tests/test_scheduler_db.py` if present, else the `db_conn` fixture with `engine` from `conftest.py`)

**Interfaces:**
- Consumes: `FdnClient`, `FdnPremarketProvider` (Tasks 2–4), `config.FDN_API_KEY` (Task 1).
- Produces: `ingest_premarket_for_session(..., client: FdnClient | None = None)` — live when `client` is passed **or** `config.FDN_API_KEY` is set; `store_captured_payloads(conn: Connection, client: FdnClient, *, as_of: date) -> int`. Task 7 passes `client` down from `run_open_session_job`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scheduler_fdn.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.providers.fdn import FdnClient, store_captured_payloads

_SESSION = date(2026, 8, 14)


def test_store_captured_payloads_is_verbatim_and_idempotent(db_conn: Connection) -> None:
    body = '[{"trading_symbol": "ZFDN", "close": 74.31}]'
    client = FdnClient("k", transport=httpx.MockTransport(
        lambda _r: httpx.Response(200, text=body)
    ))
    client.fetch("latest-prices", identifier="ZFDN")

    assert store_captured_payloads(db_conn, client, as_of=_SESSION) == 1
    assert store_captured_payloads(db_conn, client, as_of=_SESSION) == 0  # conflict-skip

    row = db_conn.execute(text(
        "SELECT source, endpoint, symbol, body FROM raw_payloads "
        "WHERE source = 'fdn' AND symbol = 'ZFDN' AND as_of = :d"
    ), {"d": _SESSION}).mappings().one()
    assert row["endpoint"] == "latest-prices"
    assert row["body"] == [{"trading_symbol": "ZFDN", "close": 74.31}]
```

And an integration test that the live branch engages (monkeypatched key, mocked transport) — assert `ingest_premarket_for_session(engine, session_date=…, prior_session=…, client=client)` writes a `quotes` row whose `extended_last` came from the mocked minute bars, not from a hash. Reuse the seeding helpers `tests/test_scheduler_db.py` (or `test_assemble_open_db.py`) already uses to plant a holding and a prior `bars_daily` close; keep symbols `Z`-prefixed.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scheduler_fdn.py -v`
Expected: FAIL — `ImportError: cannot import name 'store_captured_payloads'`

- [ ] **Step 3: Implement**

In `worker/providers/fdn.py` add (imports: `sqlalchemy.text`, `Connection`):

```python
_INSERT_RAW = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, as_of, body)
    VALUES ('fdn', :endpoint, :symbol, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, as_of) DO NOTHING
""")


def store_captured_payloads(conn: Connection, client: FdnClient, *, as_of: date) -> int:
    """Invariant 5 for the fdn feeds: every captured response, verbatim.
    Batch endpoints store under the joined identifiers string; symbol-less
    endpoints (calendars, news) under '*'. Returns new rows written."""
    written = 0
    for endpoint, symbol, body in client.captured:
        result = conn.execute(
            _INSERT_RAW,
            {"endpoint": endpoint, "symbol": symbol, "as_of": as_of, "body": body},
        )
        written += result.rowcount
    return written
```

In `worker/scheduler.py`, change `ingest_premarket_for_session`'s signature to add
`client: "FdnClient | None" = None` (import under `TYPE_CHECKING` or locally), and replace the provider-construction block (lines 400–401):

```python
    from worker.providers.fdn import FdnClient, FdnPremarketProvider, store_captured_payloads

    live_client = client or (FdnClient() if config.FDN_API_KEY else None)
    if live_client is not None:
        # Live (M16): held prev_closes still come from bars_daily — the one
        # authoritative base — while tape rows derive theirs from the vendor,
        # so TAPE_SEED_LEVELS is never consulted on this branch.
        held_provider = provider or FdnPremarketProvider(live_client, held_closes, session_date)
        tape_provider = provider or FdnPremarketProvider(live_client, {}, session_date)
    else:
        held_provider = provider or SyntheticPremarketProvider(held_closes, session_date)
        tape_provider = provider or SyntheticPremarketProvider(tape_closes, session_date)
```

and after the two `ingest_premarket` calls inside the same `engine.begin()` block, before `return written`:

```python
        if live_client is not None:
            store_captured_payloads(conn, live_client, as_of=session_date)
```

Also update the `TAPE_SEED_LEVELS` comment in `constants.py` (lines 50–66): replace its final sentence ("This dict disappears the day a licensed…") with "Consulted only while `FDN_API_KEY` is unset (M16): the live branch derives tape bases from the vendor and never reads this dict."

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_scheduler_fdn.py tests/test_scheduler.py tests/test_scheduler_db.py -v` (whichever scheduler suites exist)
Expected: PASS — and the pre-existing scheduler tests stay green because `FDN_API_KEY` is unset.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/scheduler.py worker/providers/fdn.py worker/constants.py tests/test_scheduler_fdn.py
git commit -m "feat(m16): scheduler live/synthetic branch + verbatim fdn raw-payload capture"
```

---

### Task 6: `events_fdn.py` — the live §4 calendar

**Files:**
- Create: `worker/events_fdn.py`
- Modify: `worker/providers/fdn.py` (implement `FdnProvider.earnings_calendar` is **not** needed — `events_fdn` calls `FdnClient` directly; leave the `FdnProvider` stubs alone)
- Test: `tests/test_events_fdn.py` (create)

**Interfaces:**
- Consumes: `FdnClient.fetch` (Task 2), `events_seed.CalendarEvent` and `events_seed._UPSERT` (existing), `assemble_open._CALENDAR_WINDOW_DAYS = 7` (existing — import the module constant, don't redeclare).
- Produces: `fetch_calendar_events(client: FdnClient, *, session_date: date, symbols: set[str]) -> list[CalendarEvent]` and `ingest_events_for_session(engine: Engine, client: FdnClient, *, session_date: date, user_id: str = DEV_USER_ID) -> int`. Task 7 wires the latter into `run_open_session_job`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_events_fdn.py
from __future__ import annotations

from datetime import date

import httpx

from worker.events_fdn import fetch_calendar_events
from worker.events_seed import CalendarEvent
from worker.providers.fdn import FdnClient

_SESSION = date(2026, 8, 14)


def _client(responses: dict[str, str]) -> FdnClient:
    """Route by endpoint path; any date param gets the same canned body."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=responses.get(endpoint, "[]"))
    return FdnClient("k", transport=httpx.MockTransport(handler))


def test_calendar_events_map_to_the_seed_shape_and_filter_to_the_book() -> None:
    client = _client({
        "earnings-calendar": (
            '[{"trading_symbol": "ZHELD", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2026-08-14"},'
            ' {"trading_symbol": "ZOTHER", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2026-08-14"}]'
        ),
        "dividends-calendar": (
            '[{"trading_symbol": "ZHELD", "ex_dividend_date": "2026-08-17"}]'
        ),
        "economic-calendar": (
            '[{"indicator_name": "CPI (m/m)", "country_code": "US",'
            '  "release_date": "2026-08-14"},'
            ' {"indicator_name": "ECB rate decision", "country_code": "EU",'
            '  "release_date": "2026-08-14"}]'
        ),
    })
    events = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert CalendarEvent("ZHELD", "earnings", date(2026, 8, 14), "ZHELD Q2 earnings") in events
    assert CalendarEvent("ZHELD", "ex_div", date(2026, 8, 17), "ZHELD ex-dividend") in events
    assert CalendarEvent(None, "macro", date(2026, 8, 14), "CPI (m/m)") in events
    symbols = {e.symbol for e in events}
    assert "ZOTHER" not in symbols                      # not in the book
    assert all(e.label != "ECB rate decision" for e in events)  # non-US macro dropped
    assert all(e.event_type != "lockup" for e in events)        # honestly absent live


def test_a_failed_calendar_endpoint_degrades_to_what_fetched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("economic-calendar"):
            return httpx.Response(500)
        return httpx.Response(200, text="[]")

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    assert fetch_calendar_events(client, session_date=_SESSION, symbols=set()) == []
```

Plus a `db_conn` test: `ingest_events_for_session` writes rows readable back from `events` and is idempotent on re-run (mirror `test_events_seed.py::test_seed_events_is_idempotent`, using a mocked client whose earnings body names one `Z`-symbol).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_events_fdn.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.events_fdn'`

- [ ] **Step 3: Implement**

```python
# worker/events_fdn.py
"""Live §4 calendar (M16): fdn's three calendar endpoints → `events`.

Replaces `events_seed` when FDN_API_KEY is set, one day-query per date in the
§4 window (the endpoints take a single `date`). The mapping targets exactly
the seed's vendor shape, so `assemble_open._calendar` needs no change. Lockup
expiries are covered by no vendor tier (docs/02) — in live mode they are
honestly absent rather than invented. Failures are per-endpoint and non-fatal:
§4 renders whatever fetched.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
from sqlalchemy.engine import Engine

from worker.assemble_open import _CALENDAR_WINDOW_DAYS
from worker.constants import DEV_USER_ID
from worker.events_seed import _UPSERT, CalendarEvent
from worker.providers.fdn import FdnClient


def fetch_calendar_events(
    client: FdnClient, *, session_date: date, symbols: set[str]
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for offset in range(_CALENDAR_WINDOW_DAYS + 1):
        d = session_date + timedelta(days=offset)
        events += _earnings(client, d, symbols)
        events += _ex_dividends(client, d, symbols)
        events += _macro(client, d)
    seen: set[tuple[str | None, str, date]] = set()
    unique: list[CalendarEvent] = []
    for e in events:
        key = (e.symbol, e.event_type, e.occurs_at)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _earnings(client: FdnClient, d: date, symbols: set[str]) -> list[CalendarEvent]:
    try:
        records = client.fetch("earnings-calendar", date=d.isoformat())
    except httpx.HTTPError:
        return []
    return [
        CalendarEvent(
            sym, "earnings",
            date.fromisoformat(str(r["earnings_announcement_date"])),
            f"{sym} {r.get('fiscal_period') or ''} earnings".replace("  ", " "),
        )
        for r in records
        if (sym := str(r.get("trading_symbol"))) in symbols
        and r.get("earnings_announcement_date")
    ]


def _ex_dividends(client: FdnClient, d: date, symbols: set[str]) -> list[CalendarEvent]:
    try:
        records = client.fetch("dividends-calendar", date=d.isoformat())
    except httpx.HTTPError:
        return []
    return [
        CalendarEvent(
            sym, "ex_div", date.fromisoformat(str(r["ex_dividend_date"])),
            f"{sym} ex-dividend",
        )
        for r in records
        if (sym := str(r.get("trading_symbol"))) in symbols and r.get("ex_dividend_date")
    ]


def _macro(client: FdnClient, d: date) -> list[CalendarEvent]:
    try:
        records = client.fetch("economic-calendar", date=d.isoformat())
    except httpx.HTTPError:
        return []
    return [
        CalendarEvent(
            None, "macro", date.fromisoformat(str(r["release_date"])),
            str(r["indicator_name"]),
        )
        for r in records
        if str(r.get("country_code")) == "US"
        and r.get("release_date") and r.get("indicator_name")
    ]


def ingest_events_for_session(
    engine: Engine, client: FdnClient, *, session_date: date, user_id: str = DEV_USER_ID
) -> int:
    """Fetch the window's calendars for the book's symbols and upsert into
    `events` on the seed's (symbol, event_type, occurs_at) key."""
    from worker.scheduler import book_symbols

    with engine.connect() as conn:
        held = set(book_symbols(conn, user_id))
    events = fetch_calendar_events(client, session_date=session_date, symbols=held)
    with engine.begin() as conn:
        for e in events:
            conn.execute(_UPSERT, {
                "symbol": e.symbol, "event_type": e.event_type,
                "occurs_at": e.occurs_at, "label": e.label,
            })
    return len(events)
```

(If ruff objects to importing the private `_UPSERT`/`_CALENDAR_WINDOW_DAYS`, rename them public in their home modules — `UPSERT_EVENT`, `CALENDAR_WINDOW_DAYS` — and update the existing references; do not duplicate them.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_events_fdn.py tests/test_events_seed.py -v` — Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/events_fdn.py tests/test_events_fdn.py
git commit -m "feat(m16): live §4 calendar — fdn calendars mapped onto the events seed shape"
```

---

### Task 7: News — `has_news` gate, narration headlines, and the open-job wiring

**Files:**
- Create: `worker/news_fdn.py`
- Modify: `worker/assemble_open.py:110-125` (`assemble_open` signature), `:215` (`_premarket` gate), `:448-475` (`assemble_open_and_store`)
- Modify: `worker/narrate.py:98,149` (`build_open_prompt`, `narrate_open_and_apply`)
- Modify: `worker/scheduler.py:419-495` (`run_open_session_job`)
- Test: `tests/test_news_fdn.py` (create), `tests/test_narrate.py` and `tests/test_assemble_open.py` (append)

**Interfaces:**
- Consumes: `FdnClient.fetch` (Task 2), `ingest_events_for_session` (Task 6), `ingest_premarket_for_session(..., client=…)` (Task 5).
- Produces: `fetch_held_news(client: FdnClient, *, session_date: date, held: set[str]) -> dict[str, list[str]]` (symbol → up to 3 headlines);
  `assemble_open(..., news: dict[str, list[str]] | None = None)`;
  `assemble_open_and_store(..., news: dict[str, list[str]] | None = None)`;
  `build_open_prompt(obj, headlines: dict[str, list[str]] | None = None)`;
  `narrate_open_and_apply(obj, narrator, headlines: dict[str, list[str]] | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_fdn.py
from __future__ import annotations

from datetime import date

import httpx

from worker.news_fdn import fetch_held_news
from worker.providers.fdn import FdnClient

_SESSION = date(2026, 8, 14)


def test_held_news_filters_to_the_book_and_caps_at_three() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset", "0")
        if offset != "0":
            return httpx.Response(200, text="[]")
        return httpx.Response(200, text=(
            '[{"trading_symbols": ["ZHELD"], "article_headline": "h1"},'
            ' {"trading_symbols": ["ZHELD", "ZOTHER"], "article_headline": "h2"},'
            ' {"trading_symbols": ["ZOTHER"], "article_headline": "h3"},'
            ' {"trading_symbols": ["ZHELD"], "article_headline": "h4"},'
            ' {"trading_symbols": ["ZHELD"], "article_headline": "h5"}]'
        ))

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_held_news(client, session_date=_SESSION, held={"ZHELD"})
    assert got == {"ZHELD": ["h1", "h2", "h4"]}


def test_a_news_500_degrades_to_no_news() -> None:
    client = FdnClient("k", transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    assert fetch_held_news(client, session_date=_SESSION, held={"ZHELD"}) == {}
```

Append to `tests/test_assemble_open.py` (reuse that file's existing `PremarketQuote`-building helpers; the key assertion):

```python
def test_a_sub_threshold_name_with_news_still_gets_a_row_but_no_claim() -> None:
    quote = _quote("ZNEWS", extended_last=Decimal("100.50"), prev_close=Decimal("100.00"))
    obj = assemble_open(
        events=[], sectors=[], flags=[], holdings={"ZNEWS": "owned"}, tape=[],
        premarket=[quote], claims=emit_premarket_gap([quote]),
        news={"ZNEWS": ["Zed lands a contract"]},
        user_id=DEV_USER_ID, session_date=_SESSION, prior_session=_PRIOR,
        generated_at=_GENERATED,
    )
    pre = next(s for s in obj.sections if s.id.value == "premarket")
    assert [r.symbol for r in pre.rows] == ["ZNEWS"]   # 0.5% gap, shown via news
    assert obj.claims == []                             # news is not a directional call
```

Append to `tests/test_narrate.py`:

```python
def test_open_prompt_carries_headlines_verbatim(open_fixture_obj: BriefObject) -> None:
    prompt = build_open_prompt(open_fixture_obj, headlines={"ASTS": ["FCC approves"]})
    assert "FCC approves" in prompt
    assert build_open_prompt(open_fixture_obj) == build_open_prompt(open_fixture_obj, headlines={})
```

(Adapt fixture names to that file's existing open-brief fixture.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_news_fdn.py tests/test_assemble_open.py tests/test_narrate.py -v`
Expected: new tests FAIL (`ModuleNotFoundError` / `TypeError: unexpected keyword argument 'news'` / `'headlines'`).

- [ ] **Step 3: Implement**

`worker/news_fdn.py`:

```python
"""Held-name news (M16): fdn latest-news → the §3 has_news gate + narration.

Ten records per call with offset pagination; three pages is plenty for a
morning gate. Headlines flow to exactly two places — `clears_threshold`'s
`has_news` and the open narration prompt (docs/04 rule 2: attributing a move
to a cause is the one thing the model does better than the pipeline). They are
never rendered directly and never stored outside raw_payloads, which keeps the
redistribution surface at zero. Failures degrade to no news, never a crash.
"""

from __future__ import annotations

from datetime import date

import httpx

from worker.providers.fdn import FdnClient

_PAGES = 3
_PER_SYMBOL_CAP = 3


def fetch_held_news(
    client: FdnClient, *, session_date: date, held: set[str]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for page in range(_PAGES):
        try:
            records = client.fetch(
                "latest-news", date=session_date.isoformat(), offset=str(page * 10)
            )
        except httpx.HTTPError:
            break
        if not records:
            break
        for record in records:
            headline = str(record.get("article_headline") or "").strip()
            if not headline:
                continue
            for symbol in record.get("trading_symbols") or []:
                if str(symbol) in held and len(out.setdefault(str(symbol), [])) < _PER_SYMBOL_CAP:
                    out[str(symbol)].append(headline)
    return out
```

`worker/assemble_open.py` — three edits:

1. `assemble_open` gains `news: dict[str, list[str]] | None = None` (after `stale`), passes `frozenset(news or {})` to `_premarket`.
2. `_premarket(quotes: list[PremarketQuote], news_symbols: frozenset[str] = frozenset())` and line 215 becomes
   `shown = [q for q in quotes if clears_threshold(q, has_news=q.symbol in news_symbols)]`.
3. `assemble_open_and_store` gains the same `news` parameter, forwards it to `assemble_open(...)`, and its narration line becomes
   `obj = narrate_open_and_apply(obj, narrator, headlines=news)  # type: ignore[arg-type]`.

`worker/narrate.py` — two edits:

1. `build_open_prompt(obj: BriefObject, headlines: dict[str, list[str]] | None = None)`; before the `return`, build

```python
    news_block = ""
    if headlines:
        lines = [f"  {sym}: " + " · ".join(hs) for sym, hs in sorted(headlines.items())]
        news_block = (
            "News headlines for these names (attribute moves to causes where "
            "they explain them; do not restate figures):\n" + "\n".join(lines) + "\n\n"
        )
```

   and insert `f"{news_block}"` immediately before `"The day's setup, for context only …"`.
2. `narrate_open_and_apply(obj, narrator, headlines: dict[str, list[str]] | None = None)` forwards `headlines` into its `build_open_prompt` call.

`worker/scheduler.py` — `run_open_session_job`, after `prior = …` (line 450):

```python
        from worker.providers.fdn import FdnClient

        client = FdnClient() if config.FDN_API_KEY else None
        written = ingest_premarket_for_session(
            engine, session_date=session_date, prior_session=prior,
            user_id=user_id, client=client,
        )
        print(f"open {session_date}: captured {written} pre-market quotes.")

        news: dict[str, list[str]] = {}
        if client is not None:
            from worker.events_fdn import ingest_events_for_session
            from worker.news_fdn import fetch_held_news

            n_events = ingest_events_for_session(
                engine, client, session_date=session_date, user_id=user_id
            )
            with engine.connect() as conn:
                held = set(book_symbols(conn, user_id))
            news = fetch_held_news(client, session_date=session_date, held=held)
            with engine.begin() as conn:
                store_captured_payloads(conn, client, as_of=session_date)
            print(f"open {session_date}: {n_events} calendar events, news for {sorted(news)}.")
```

and pass `news=news` in the `assemble_open_and_store(...)` call. (`store_captured_payloads` conflict-skips rows Task 5 already stored, so the second call only adds the calendar/news captures. Import it alongside `FdnClient`.)

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -x -q`
Expected: PASS — synthetic-mode fixtures unchanged (no key set), new tests green.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/news_fdn.py worker/assemble_open.py worker/narrate.py worker/scheduler.py \
    tests/test_news_fdn.py tests/test_assemble_open.py tests/test_narrate.py
git commit -m "feat(m16): held-name news — has_news gate + narration headlines, open-job wiring"
```

---

### Task 8: `fdn-probe` — the day-one verification CLI

**Files:**
- Modify: `worker/cli.py` (new subcommand; follow the `seed-premarket` pattern at lines 47–51 / 309–321)
- Test: `tests/test_cli_fdn_probe.py` (create)

**Interfaces:**
- Consumes: `FdnClient`, `FDN_TAPE_IDENTIFIERS`.
- Produces: `uv run -m worker.cli fdn-probe [--symbols ASTS,RKLB]` — read-only, no DB writes; exits 0 with a table of ✓/✗ per identifier.

Probe checks (each prints one line, failures don't stop the run):
1. Every `FDN_TAPE_IDENTIFIERS` entry: fetch, report record count and first `trading_symbol` (verifies `ES`/`^DXY`-style guesses).
2. For one held symbol: `latest-prices`, print the two most recent `time` values next to `datetime.now(UTC)` — the reader confirms the UTC assumption in `_parse_fdn_time` on sight.
3. `futures-prices` for `ES`: print the latest bar's `date` vs today — verifies the session-dated-bar assumption at pre-open time.
4. `stock-quotes` for one proxy ETF: print the raw record keys — verifies the `price`/`change` field assumption.
5. Each calendar + `latest-news` for today: record counts (also proves the key's tier covers Premium).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_fdn_probe.py
from __future__ import annotations

import httpx

from worker.cli import _fdn_probe
from worker.providers.fdn import FdnClient


def test_probe_reports_every_route_and_survives_failures(capsys: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("futures-prices"):
            return httpx.Response(500)
        return httpx.Response(200, text='[{"trading_symbol": "X", "price": 1.0, "time": "2026-08-14 12:00:00"}]')

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    _fdn_probe(client, symbols=["ASTS"])  # must not raise
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "ES=F" in out and "✗" in out and "✓" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli_fdn_probe.py -v`
Expected: FAIL — `ImportError: cannot import name '_fdn_probe'`

- [ ] **Step 3: Implement**

In `worker/cli.py`: register the parser next to `seed-premarket`:

```python
    fdn_probe = sub.add_parser(
        "fdn-probe",
        help="verify FDN_API_KEY, identifier mapping, and feed assumptions (read-only)",
    )
    fdn_probe.add_argument("--symbols", help="held symbols to probe, comma-separated")
```

and implement `_fdn_probe(client: FdnClient, *, symbols: list[str]) -> None` running the five checks above — each in its own `try/except httpx.HTTPError`, printing `✓ <label>: <summary>` or `✗ <label>: <error>`. The `main()` branch constructs `FdnClient()` (which raises with a clear message when the key is unset) and defaults `symbols` to `_resolve_symbols(args.symbols, engine)` like `backfill` does.

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_cli_fdn_probe.py -v` — Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check worker tests && uv run mypy worker
git add worker/cli.py tests/test_cli_fdn_probe.py
git commit -m "feat(m16): fdn-probe CLI — day-one identifier and assumption verification"
```

---

### Task 9: Docs + milestone entry

**Files:**
- Modify: `docs/02-architecture.md` (vendor table), `docs/07-decisions.md` (append D25), `docs/08-milestones.md` (M16 entry), `.env.example` (FDN_API_KEY line), `apps/worker/fly.toml` (secrets comment line 4–7)

- [ ] **Step 1: docs/02 vendor table** — change the FinancialData.net row's "**not wired**" to "wired behind `FDN_API_KEY` (M16); synthetic fallback when unset", and the company-news row likewise.

- [ ] **Step 2: docs/07 decision** — append:

```markdown
## D25 — fdn is spoken directly over httpx, not through fdnpy (M16)

The fdnpy SDK parses prices as float and adds a requests dependency; the money
invariant wants `parse_float=Decimal` on every byte, and the repo already
speaks httpx to Tiingo. `FdnClient` replicates the four-line transport with
Decimal parsing and verbatim response capture (invariant 5). The vendor's
`key` query parameter is an accepted exception to the header-auth rule — fdn
has no header auth — mitigated by never logging request URLs. The live/
synthetic switch is the presence of `FDN_API_KEY`, never a hand-flipped flag:
a flag and a construction site can disagree; a derivation cannot.
```

- [ ] **Step 3: docs/08 milestone** — append after M15:

```markdown
- [ ] **M16 — Live fdn feed: §2/§3/§4 + news off the synthetic seed.** `FdnClient`
  (direct httpx, Decimal, raw-payload capture) + `FdnPremarketProvider` behind the
  M15 seam; live calendars replace `events_seed`; `latest-news` turns on §3's
  `has_news` clause and feeds narration headlines. Switched by `FDN_API_KEY`
  presence — unset stays bit-for-bit synthetic. No schema bump. *Done when:* with
  a Premium key set, `fdn-probe` reports every tape identifier resolving; the next
  morning's open brief sends with real §2 levels, real §3 gaps, a real §4 calendar,
  and **without** the "synthetic feed" banner; with the key removed the same code
  sends the synthetic brief with the banner back on; `uv run pytest` passes in
  both modes. See `docs/superpowers/specs/2026-08-14-m16-fdn-live-feed-design.md`.
```

- [ ] **Step 4: `.env.example`** — add under the market-data section:

```bash
# FinancialData.net (M16). Premium tier. Empty ⇒ open brief runs synthetic.
FDN_API_KEY=
```

and add `FDN_API_KEY=...` to the `fly secrets set` comment in `apps/worker/fly.toml`.

- [ ] **Step 5: Commit**

```bash
git add docs/02-architecture.md docs/07-decisions.md docs/08-milestones.md .env.example apps/worker/fly.toml
git commit -m "docs(m16): vendor table, D25, M16 milestone, env + fly secret stubs"
```

---

## Definition of done (mirrors the M16 milestone entry)

Offline (now): full suite green in synthetic mode; the six new test files green; `mypy --strict` + `ruff` clean.
Day the key lands: `fly secrets set FDN_API_KEY=…`, run `uv run -m worker.cli fdn-probe`, fix `FDN_TAPE_IDENTIFIERS` entries the probe flags (a constants-table edit, no code), redeploy, and confirm the next morning's email arrives without the synthetic banner.
