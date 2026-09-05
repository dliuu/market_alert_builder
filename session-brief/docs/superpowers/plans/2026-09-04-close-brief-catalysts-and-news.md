# Close-Brief Catalysts + Week News Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The closing brief flags insider catalysts (the missed ASTS buys) and carries a week of held-name news, unattended.

**Architecture:** Three broken links, fixed in order. (1) The M17 catalysts pipeline exists but has no live provider — `normalize_*` parses the *synthetic* record shape, the CLI hardwires `SyntheticCatalystProvider`, and the scheduler never runs ingest/detect at all: `catalyst_signals` is empty in production. A live `FdnCatalystProvider` returns vendor rows verbatim, `normalize_*` learns the real vendor field names (verified by live probe 2026-09-04), and the close job runs ingest + detect before assemble. (2) No current rule fires on the actual ASTS event (a *Director's* $500K open-market buy — `clevel_buy` needs CEO/CFO/president, `cluster` needs 3 distinct insiders): a new `notable_buy` rule fires on any insider open-market purchase ≥ $100K. (3) The close brief has no news: a new `fetch_week_news` pages the market-wide `latest-news` feed over the prior 7 calendar days and feeds held-name headlines to close narration as prose context — never rendered verbatim (licensing: redistribution surface stays zero).

**Tech Stack:** Python 3.14, httpx, SQLAlchemy Core, pytest with `httpx.MockTransport`; `uv` for everything.

**Spec:** No separate spec file (bounded change, design approved in chat 2026-09-04). The M17 catalysts design this completes: `docs/superpowers/specs/2026-08-14-m17-catalysts-design.md`. Probe-verified vendor facts are in the next section — they are the spec for the field mappings.

## Probe-verified vendor facts (2026-09-04, live key)

These were established by running `fdn-probe` and direct `FdnClient` calls; the field mappings below are copied from real responses, not guessed.

- `latest-news` accepts **only** `date`, `offset`, `format` (vendor docs + probe: an `identifier` param is silently dropped — 10 records returned, 0 mentioning the symbol). **Q10 answer: IGNORED; no per-symbol filter exists at any tier.** Market-wide paging is the only shape.
- One day of `latest-news` ≈ 190 records / 20 pages of 10, touching only ~27 distinct symbols. Record keys: `article_headline`, `article_text`, `publication_time` ("YYYY-MM-DD HH:MM:SS"), `trading_symbols` (list).
- Paging aggressively returned **HTTP 429**; the Premium tier's documented limit is 30 req/s (Standard 10 req/s). The week fetch throttles between pages and degrades per-day on errors.
- `insider-transactions` (`identifier=SYM`, page size 50, newest first) record keys: `trading_symbol`, `insider_name`, `relationship_to_issuer` ("Director", "Chief Technology Officer", …), `transaction_date`, `transaction_code` (**already Form 4 codes**: P, S, X, M, F, A, G…), `transaction_description`, `amount_of_securities`, `price_per_security` (Decimal), `securities_owned_following_transaction`, `acquired_or_disposed`, `is_derivatives_transaction` (bool), `ownership_form`, `nature_of_indirect_ownership`, `title_of_security`, `central_index_key`, `insider_central_index_key`, `registrant_name`. **No `filing_date` field.**
- The missed ASTS event is present in that feed: Director Adriana Cisneros, three code-P purchases dated 2026-08-31, the largest 8,768 shares × $57.00 ≈ $500K.
- `proposed-sales` (`identifier=SYM`) record keys: `trading_symbol`, `seller_name`, `relationship_to_issuer`, `broker_name`, `amount_of_securities_to_be_sold`, `market_value`, `amount_of_securities_outstanding`, `approximate_date_of_sale`, `acquisition_period_start`, `acquisition_period_end`, `nature_of_acquisition_transaction`, `names_of_persons_from_whom_acquired`, plus registrant/CIK/exchange fields. **No `filing_date` field.**

## Global Constraints

- All worker commands run from `apps/worker`: `uv run pytest`, `uv run ruff check`, `uv run mypy` (strict mode is configured in `pyproject.toml`).
- Money is integer cents computed through `Decimal`, never float (repo invariant; `normalize_insider`'s existing comment).
- Raw vendor bodies are stored **verbatim** in `raw_payloads` (invariant 5) — providers must return vendor rows unmapped; `normalize_*` owns all field mapping and stays pure (no network, no DB).
- Vendor failures degrade a section or a line, never kill a run (`FEED_ERRORS` pattern in `worker/providers/fdn.py`).
- Never log request URLs or exception text that may embed the fdn key (see `_safe_error` in `worker/cli.py`).
- Headlines are narration context only — never rendered in the email, never stored outside `raw_payloads` (`worker/news_fdn.py` header).
- Match existing commit style: `feat(catalysts): …`, `test(news): …` etc., short imperative subject.

---

### Task 1: `normalize_insider` parses the live vendor shape

The synthetic provider is a stand-in for the licensed feed, so it must emit the *vendor's* field names and `normalize_insider` must parse only that shape. This is what makes replay-from-`raw_payloads` true for live data.

**Files:**
- Modify: `worker/catalysts_ingest.py` (`_TYPE_MAP`, `normalize_insider`)
- Modify: `worker/providers/synthetic.py:122-148` (`SyntheticCatalystProvider.insider_transactions`)
- Test: `tests/test_catalysts_ingest.py`

**Interfaces:**
- Consumes: `InsiderTx` dataclass (unchanged, `worker/catalysts.py:53`).
- Produces: `normalize_insider(rows: list[dict[str, Any]]) -> list[InsiderTx]` — same signature, now expecting keys `trading_symbol`, `insider_name`, `relationship_to_issuer`, `transaction_date`, `transaction_code`, `amount_of_securities`, `price_per_security`, `securities_owned_following_transaction`, `is_derivatives_transaction`. Rows with a truthy `is_derivatives_transaction` are skipped. `filing_date` on the dataclass is set to `transaction_date` (vendor sends none).

- [ ] **Step 1: Write the failing tests**

Replace the two shape-dependent tests in `tests/test_catalysts_ingest.py` (`test_the_synthetic_provider_emits_the_documented_shape`, `test_normalize_maps_vendor_types_onto_form_4_codes`) and add the derivative-skip test:

```python
def test_the_synthetic_provider_emits_the_vendor_shape() -> None:
    rows = SyntheticCatalystProvider(_D).insider_transactions("SNDK")

    assert rows, "a seeded symbol must produce filings or the section can't be developed"
    row = rows[0]
    assert {"trading_symbol", "insider_name", "relationship_to_issuer",
            "transaction_date", "transaction_code", "amount_of_securities",
            "price_per_security", "securities_owned_following_transaction",
            "is_derivatives_transaction"} <= set(row)


def test_normalize_parses_the_vendor_record() -> None:
    """Field names and types are copied from a live 2026-09-04 probe of
    `insider-transactions identifier=ASTS` — the shape is a fact, not a guess."""
    txs = normalize_insider([{
        "trading_symbol": "ASTS", "insider_name": "Cisneros Adriana",
        "relationship_to_issuer": "Director", "transaction_date": "2026-08-31",
        "transaction_code": "P", "amount_of_securities": 8768,
        "price_per_security": Decimal("57.0"), "acquired_or_disposed": "A",
        "securities_owned_following_transaction": 796353,
        "is_derivatives_transaction": False, "ownership_form": "I",
    }])

    t = txs[0]
    assert t.symbol == "ASTS"
    assert t.insider_title == "Director"
    assert t.transaction_code == "P"
    assert t.value_cents == 49_977_600  # 8768 * $57.00, integer cents
    assert t.shares_after == Decimal(796353)
    assert t.filing_date == t.transaction_date  # vendor sends no filing date


def test_normalize_skips_derivative_legs_and_maps_x_to_exercise() -> None:
    """An option exercise arrives as two rows: a derivative leg (skipped —
    counting both would double every exercise) and a non-derivative leg whose
    code X is mechanical, like M — a direction-less transaction."""
    txs = normalize_insider([
        {"trading_symbol": "ASTS", "insider_name": "Yao Huiwen",
         "relationship_to_issuer": "Chief Technology Officer",
         "transaction_date": "2026-08-19", "transaction_code": "X",
         "amount_of_securities": 40000, "price_per_security": Decimal("0.0"),
         "is_derivatives_transaction": True},
        {"trading_symbol": "ASTS", "insider_name": "Yao Huiwen",
         "relationship_to_issuer": "Chief Technology Officer",
         "transaction_date": "2026-08-19", "transaction_code": "X",
         "amount_of_securities": 40000, "price_per_security": Decimal("0.0641"),
         "securities_owned_following_transaction": 74750,
         "is_derivatives_transaction": False},
    ])

    assert len(txs) == 1
    assert txs[0].transaction_code == "M"  # X = exercise, mechanical, no direction
```

Keep `from decimal import Decimal` (already imported in the test file).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalysts_ingest.py -v`
Expected: the three new tests FAIL (`KeyError: 'trading_symbol'` from normalize; missing keys from the synthetic provider). Pre-existing tests still pass.

- [ ] **Step 3: Implement**

In `worker/catalysts_ingest.py`, add to `_TYPE_MAP` (the vendor's single-letter Form 4 codes already round-trip through it via `.lower()` — "P"→"p"→"P"):

```python
    "x": "M", "exercise of in-the-money or at-the-money derivative security": "M",
```

Replace the body of `normalize_insider`:

```python
def normalize_insider(rows: list[dict[str, Any]]) -> list[InsiderTx]:
    """Vendor shape -> typed rows. Pure. Money lands as integer cents via
    Decimal, never float: ``3 * 0.10`` is 30 cents, not 30.000000000000004.

    Field names are the live feed's (probed 2026-09-04): ``trading_symbol``,
    ``amount_of_securities``, ``price_per_security``,
    ``securities_owned_following_transaction``, ``relationship_to_issuer``.
    The vendor sends no filing date, so ``filing_date`` carries the
    transaction date. Derivative legs are skipped: an exercise arrives as a
    derivative and a non-derivative row for the same shares, and counting
    both would double it in cluster and cadence math."""
    out: list[InsiderTx] = []
    for r in rows:
        if r.get("is_derivatives_transaction"):
            continue
        shares = _decimal(r.get("amount_of_securities")) or Decimal(0)
        price = _decimal(r.get("price_per_security")) or Decimal(0)
        raw_type = str(r.get("transaction_code") or "").strip().lower()
        title = r.get("relationship_to_issuer")
        out.append(InsiderTx(
            symbol=str(r["trading_symbol"]),
            insider_name=str(r.get("insider_name") or "unknown"),
            insider_title=(str(title) if title else None),
            transaction_date=date.fromisoformat(str(r["transaction_date"])),
            filing_date=date.fromisoformat(str(r["transaction_date"])),
            transaction_code=_TYPE_MAP.get(raw_type, "?"),
            shares=shares,
            value_cents=int((shares * price * 100).to_integral_value()),
            shares_after=_decimal(r.get("securities_owned_following_transaction")),
            row_id=0,  # assigned by the database on insert
        ))
    return out
```

In `worker/providers/synthetic.py`, rewrite the row dict in `insider_transactions` to the vendor shape (values and determinism unchanged — only key names move):

```python
            rows.append({
                "trading_symbol": symbol,
                "insider_name": name,
                "relationship_to_issuer": title,
                "transaction_date": on.isoformat(),
                "transaction_code": _CODES[int(self._unit(symbol, f"code{i}") * len(_CODES))],
                "amount_of_securities": str(shares),
                "price_per_security": str(price),
                "is_derivatives_transaction": False,
                # The remaining holding has to *vary*: a fixed multiple would pin
                # pct_of_holding at one value and `outsized_sale` (>=40%) could
                # never fire on seeded data, leaving a whole rule undevelopable.
                # 0.4x-6x spans both sides of the threshold.
                "securities_owned_following_transaction": str(
                    (shares * (Decimal("0.4") + self._unit(symbol, f"after{i}") * 6))
                    .quantize(Decimal("1"))
                ),
            })
```

(`_CODES` already holds single letters — they map through `_TYPE_MAP` unchanged.)

- [ ] **Step 4: Run the full ingest + catalysts test files**

Run: `uv run pytest tests/test_catalysts_ingest.py tests/test_catalysts.py tests/test_catalysts_db.py tests/test_catalysts_assemble.py -v`
Expected: PASS. If another test feeds `normalize_insider` the old shape, update its fixture keys to the vendor names — the values and assertions stay the same.

- [ ] **Step 5: Commit**

```bash
git add worker/catalysts_ingest.py worker/providers/synthetic.py tests/test_catalysts_ingest.py
git commit -m "feat(catalysts): normalize_insider parses the live vendor shape"
```

---

### Task 2: `normalize_proposed` parses the live vendor shape

**Files:**
- Modify: `worker/catalysts_ingest.py` (`normalize_proposed`)
- Modify: `worker/providers/synthetic.py:150-165` (`SyntheticCatalystProvider.proposed_sales`)
- Test: `tests/test_catalysts_ingest.py`

**Interfaces:**
- Consumes: `ProposedSale` dataclass (unchanged, `worker/catalysts.py:67`: `symbol, insider_name, filing_date, shares_proposed, approx_sale_date, row_id`).
- Produces: `normalize_proposed(rows) -> list[ProposedSale]` expecting vendor keys `trading_symbol`, `seller_name`, `amount_of_securities_to_be_sold`, `approximate_date_of_sale` (fallback `acquisition_period_end`; a record with neither is skipped). Both `filing_date` and `approx_sale_date` carry that date — the vendor exposes no filing date.

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_proposed_parses_the_vendor_record() -> None:
    """Live 2026-09-04 probe of `proposed-sales identifier=ASTS`. The vendor
    sends no filing date; the approximate sale date anchors the row, so
    `unconverted_144` reads "past the stated sale date and still unexecuted"."""
    sales = normalize_proposed([{
        "trading_symbol": "ASTS", "seller_name": "AA Gables 2, LLC",
        "relationship_to_issuer": "(1)", "broker_name": "Citigroup Global Markets Inc.",
        "amount_of_securities_to_be_sold": 2500000,
        "market_value": Decimal("182975000.0"),
        "amount_of_securities_outstanding": 298746383,
        "approximate_date_of_sale": "2026-06-22",
        "acquisition_period_start": "2026-06-22", "acquisition_period_end": "2026-06-22",
    }])

    s = sales[0]
    assert s.symbol == "ASTS"
    assert s.insider_name == "AA Gables 2, LLC"
    assert s.shares_proposed == Decimal(2500000)
    assert s.filing_date == date(2026, 6, 22)
    assert s.approx_sale_date == date(2026, 6, 22)


def test_normalize_proposed_skips_a_record_with_no_date() -> None:
    """A 144 that can't be dated can't be keyed, aged, or matched to a Form 4 —
    skipping it is recorded honesty, inventing a date is not."""
    assert normalize_proposed([{
        "trading_symbol": "ASTS", "seller_name": "X",
        "amount_of_securities_to_be_sold": 100,
        "approximate_date_of_sale": None, "acquisition_period_end": None,
    }]) == []
```

Add `from worker.catalysts_ingest import normalize_proposed` and `from datetime import date` to the imports if absent.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalysts_ingest.py -v -k proposed`
Expected: FAIL with `KeyError` (old shape expects `symbol`/`filing_date`).

- [ ] **Step 3: Implement**

Replace `normalize_proposed` in `worker/catalysts_ingest.py`:

```python
def normalize_proposed(rows: list[dict[str, Any]]) -> list[ProposedSale]:
    """Vendor shape -> typed rows. Pure. The vendor exposes no filing date;
    ``approximate_date_of_sale`` (falling back to ``acquisition_period_end``)
    anchors the row as both ``filing_date`` and ``approx_sale_date``, which
    shifts ``unconverted_144``'s clock to "past the stated sale date and still
    unexecuted" — arguably the more meaningful reading. A record with neither
    date is skipped: it cannot be keyed, aged, or matched."""
    out: list[ProposedSale] = []
    for r in rows:
        sale_date = r.get("approximate_date_of_sale") or r.get("acquisition_period_end")
        if not sale_date:
            continue
        out.append(ProposedSale(
            symbol=str(r["trading_symbol"]),
            insider_name=str(r.get("seller_name") or "unknown"),
            filing_date=date.fromisoformat(str(sale_date)),
            shares_proposed=_decimal(r.get("amount_of_securities_to_be_sold")) or Decimal(0),
            approx_sale_date=date.fromisoformat(str(sale_date)),
            row_id=0,
        ))
    return out
```

In `worker/providers/synthetic.py`, rename the keys in `proposed_sales`'s returned dict to the vendor shape: `symbol` → `trading_symbol`, `insider_name` → `seller_name`, `shares_proposed` → `amount_of_securities_to_be_sold`, and replace the `filing_date` key with `approximate_date_of_sale` (same generated value). Keep the generated values and comments byte-for-byte.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_catalysts_ingest.py tests/test_catalysts.py -v`
Expected: PASS (update any remaining old-shape fixtures to the vendor keys, values unchanged).

- [ ] **Step 5: Commit**

```bash
git add worker/catalysts_ingest.py worker/providers/synthetic.py tests/test_catalysts_ingest.py
git commit -m "feat(catalysts): normalize_proposed parses the live vendor shape"
```

---

### Task 3: `FdnCatalystProvider` — the live seam

**Files:**
- Modify: `worker/providers/fdn.py` (new class, after `FdnPremarketProvider`)
- Create: `tests/test_fdn_catalyst_provider.py`

**Interfaces:**
- Consumes: `FdnClient.fetch(endpoint, **params) -> list[dict]` (`worker/providers/fdn.py:88`), `CatalystProvider` protocol (`worker/providers/base.py:56`).
- Produces: `FdnCatalystProvider(client: FdnClient)` with `insider_transactions(symbol, *, offset=0)`, `proposed_sales(symbol, *, offset=0)`, `public_float(symbol) -> Decimal | None` (always `None`). Rows are vendor-verbatim. Task 6 and Task 8 construct it.

- [ ] **Step 1: Write the failing tests**

```python
"""M17's live provider seam: FdnClient -> CatalystProvider, rows verbatim."""

from __future__ import annotations

import httpx

from worker.providers.fdn import FdnCatalystProvider, FdnClient


def test_insider_transactions_hit_the_endpoint_and_return_vendor_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text='[{"trading_symbol": "ASTS", "transaction_code": "P"}]')

    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(handler)))
    rows = provider.insider_transactions("ASTS")

    assert rows == [{"trading_symbol": "ASTS", "transaction_code": "P"}]  # verbatim
    assert seen[0].url.path.endswith("/insider-transactions")
    assert seen[0].url.params["identifier"] == "ASTS"
    assert seen[0].url.params["offset"] == "0"


def test_proposed_sales_hit_their_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="[]")

    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(handler)))
    assert provider.proposed_sales("ASTS", offset=100) == []
    assert seen[0].url.path.endswith("/proposed-sales")
    assert seen[0].url.params["offset"] == "100"


def test_public_float_is_none() -> None:
    """Q4 stands: the vendor's float is unproven, so `large_144` keeps its
    conservative `fundamentals.shares_out` denominator from `book_floats`."""
    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(
        lambda _r: httpx.Response(500)
    )))
    assert provider.public_float("ASTS") is None  # no network call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fdn_catalyst_provider.py -v`
Expected: FAIL with `ImportError: cannot import name 'FdnCatalystProvider'`.

- [ ] **Step 3: Implement**

Add to `worker/providers/fdn.py` (after `FdnPremarketProvider`; `Decimal` is already imported):

```python
class FdnCatalystProvider:
    """The live ``CatalystProvider`` (M17's seam, D30) over ``FdnClient``.

    Rows are returned vendor-verbatim: invariant 5 stores exactly what came
    over the wire, and ``worker/catalysts_ingest.normalize_*`` owns every
    field mapping. ``public_float`` is ``None`` per open question 4's answer —
    ``large_144``'s denominator stays the conservative
    ``fundamentals.shares_out`` read (``book_floats``)."""

    def __init__(self, client: FdnClient) -> None:
        self._client = client

    def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        return self._client.fetch("insider-transactions", identifier=symbol, offset=str(offset))

    def proposed_sales(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        return self._client.fetch("proposed-sales", identifier=symbol, offset=str(offset))

    def public_float(self, symbol: str) -> Decimal | None:
        return None
```

- [ ] **Step 4: Run tests + mypy**

Run: `uv run pytest tests/test_fdn_catalyst_provider.py -v && uv run mypy worker/providers/fdn.py`
Expected: PASS; mypy clean (the class must structurally satisfy `CatalystProvider`).

- [ ] **Step 5: Commit**

```bash
git add worker/providers/fdn.py tests/test_fdn_catalyst_provider.py
git commit -m "feat(catalysts): FdnCatalystProvider, the live seam over FdnClient"
```

---

### Task 4: per-symbol isolation covers parse errors too

`_ingest_one` catches only the *fetch*; `store` (which runs `normalize_*`) sits outside the `try`, so one malformed vendor record would abort the whole run — exactly what the watermark machinery was built to prevent, and live data is where malformed records happen.

**Files:**
- Modify: `worker/catalysts_ingest.py` (`_ingest_one`)
- Test: `tests/test_catalysts_ingest.py`

**Interfaces:**
- Consumes/Produces: `_ingest_one` signature unchanged; behavior change only — a raising `store` marks the watermark failed and returns 0 instead of propagating.

- [ ] **Step 1: Write the failing test**

Find the existing `ingest_catalysts` DB-backed test in `tests/test_catalysts_ingest.py` (or `tests/test_catalysts_db.py`) to copy its connection fixture pattern, then add:

```python
def test_a_malformed_record_fails_one_symbol_not_the_run(conn) -> None:
    """`store` runs `normalize_*`; a record the mapper cannot parse must land
    on the watermark like a vendor 500 does, not abort every later symbol."""
    class _OneBadApple:
        def insider_transactions(self, symbol: str, *, offset: int = 0):
            if symbol == "BAD":
                return [{"trading_symbol": "BAD"}]  # no transaction_date -> parse error
            return []

        def proposed_sales(self, symbol: str, *, offset: int = 0):
            return []

        def public_float(self, symbol: str):
            return None

    counts = ingest_catalysts(conn, _OneBadApple(), ["BAD", "ZOK"], as_of=_D)

    assert counts == {"insider": 0, "proposed": 0}
    row = conn.execute(text(
        "SELECT consecutive_fails FROM catalyst_watermarks "
        "WHERE source = 'insider' AND symbol = 'BAD'"
    )).scalar_one()
    assert row == 1
```

(Adapt the fixture name to the file's convention — the existing DB tests show it; they gate on a test database the same way `tests/test_catalysts_db.py` does.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_catalysts_ingest.py -v -k malformed`
Expected: FAIL — `KeyError: 'transaction_date'` propagates out of `ingest_catalysts`.

- [ ] **Step 3: Implement**

In `_ingest_one`, move the payload insert and the store call inside the `try`:

```python
    try:
        rows = fetch(symbol)
        if rows:
            conn.execute(_INSERT_PAYLOAD, {
                "endpoint": endpoint, "symbol": symbol, "as_of": as_of,
                "body": json.dumps(rows, default=str),
            })
        stored = int(store(rows) or 0)
    except Exception as exc:  # noqa: BLE001 - one symbol's failure is not the run's
        conn.execute(_MARK_FAIL, {"source": source, "symbol": symbol, "error": str(exc)})
        return 0

    conn.execute(_MARK_OK, {"source": source, "symbol": symbol, "now": now, "as_of": as_of})
    return stored
```

(Python-level parse errors leave the transaction usable, so `_MARK_FAIL` still lands; a DB-level error inside `store` would poison the transaction either way, same as before this change.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_catalysts_ingest.py tests/test_catalysts_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/catalysts_ingest.py tests/test_catalysts_ingest.py
git commit -m "fix(catalysts): a parse error fails one symbol's watermark, not the run"
```

---

### Task 5: the `notable_buy` rule

The actual ASTS event — a Director's ~$500K open-market buy — fires **no** current rule: `clevel_buy` needs a CEO/CFO/president title, `cluster` needs 3 distinct insiders. Any insider committing six figures of their own money open-market is the catalyst readers want flagged, whatever the title.

**Files:**
- Modify: `worker/constants.py` (§5.1 block, after `CLEVEL_BUY_SEVERITY`)
- Modify: `worker/catalysts.py` (`detect_insider`)
- Modify: `apps/web/emails/close-brief.tsx` (`catalystLine`) and `apps/web/emails/cn/close-brief.tsx` if it carries its own `catalystLine` switch (check with `grep -n catalystLine apps/web/emails/cn/close-brief.tsx` from the repo root; mirror the case if so)
- Test: `tests/test_catalysts.py`

**Interfaces:**
- Consumes: `_emit`, `_direction`, `_is_clevel` (`worker/catalysts.py`), `CatalystSignal`.
- Produces: signals with `kind="notable_buy"`, `severity=NOTABLE_BUY_SEVERITY` (4 → "full" tier via `_SEVERITY_TIER`), `detail={"insider_count": 1, "total_value_cents": …}`. New constants `NOTABLE_BUY_SEVERITY = 4`, `NOTABLE_BUY_MIN_CENTS = 10_000_000`. `Row.kind` in the contract is a plain `str | None` (`contracts/brief.py:221`) so no contract change; `catalysts_section._row` is kind-agnostic, no renderer-side Python change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_catalysts.py`, following its existing `InsiderTx` construction style (copy a neighboring test's helper/fixture):

```python
def test_a_directors_large_open_market_buy_is_notable() -> None:
    """The missed ASTS case, verbatim: Director, code P, ~$500K (2026-08-31).
    Neither clevel_buy (title) nor cluster (one name) covers it."""
    t = InsiderTx(
        symbol="ASTS", insider_name="Cisneros Adriana", insider_title="Director",
        transaction_date=date(2026, 8, 31), filing_date=date(2026, 8, 31),
        transaction_code="P", shares=Decimal(8768),
        value_cents=49_977_600, shares_after=Decimal(796353), row_id=1,
    )

    signals = detect_insider([t])

    kinds = {s.kind for s in signals}
    assert "notable_buy" in kinds
    [sig] = [s for s in signals if s.kind == "notable_buy"]
    assert sig.severity == NOTABLE_BUY_SEVERITY
    assert sig.detail == {"insider_count": 1, "total_value_cents": 49_977_600}


def test_a_small_buy_and_a_clevel_buy_are_not_notable() -> None:
    """Below threshold stays quiet; a C-level buy keeps its own (higher)
    severity rather than double-emitting."""
    small = InsiderTx(
        symbol="ASTS", insider_name="Cisneros Adriana", insider_title="Director",
        transaction_date=date(2026, 8, 31), filing_date=date(2026, 8, 31),
        transaction_code="P", shares=Decimal(670),
        value_cents=3_944_290, shares_after=Decimal(797023), row_id=2,
    )
    ceo = InsiderTx(
        symbol="ZC", insider_name="Dana Whitfield", insider_title="Chief Executive Officer",
        transaction_date=date(2026, 8, 31), filing_date=date(2026, 8, 31),
        transaction_code="P", shares=Decimal(10000),
        value_cents=50_000_000, shares_after=Decimal(100000), row_id=3,
    )

    kinds = [(s.symbol, s.kind) for s in detect_insider([small, ceo])]

    assert ("ASTS", "notable_buy") not in kinds
    assert ("ZC", "clevel_buy") in kinds
    assert ("ZC", "notable_buy") not in kinds
```

Import `NOTABLE_BUY_SEVERITY` from `worker.constants` at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalysts.py -v -k notable`
Expected: FAIL with `ImportError` on `NOTABLE_BUY_SEVERITY`.

- [ ] **Step 3: Implement**

`worker/constants.py`, in the §5.1 insider block after `CLEVEL_BUY_SEVERITY = 5`:

```python
NOTABLE_BUY_SEVERITY = 4
# Any insider's open-market purchase at or above this commits enough of their
# own money to be a catalyst whatever the title — the rule that catches a
# Director's buy, which clevel_buy (title-gated) and cluster (>=3 names) miss.
NOTABLE_BUY_MIN_CENTS = 10_000_000  # $100k
```

`worker/catalysts.py`: import the two constants alongside the existing ones, and in `detect_insider` replace the buy branch:

```python
        if direction == "buy":
            if _is_clevel(t.insider_title):
                signals.append(_emit(t, "clevel_buy", CLEVEL_BUY_SEVERITY,
                                     {"insider_count": 1, "total_value_cents": t.value_cents}))
            elif t.value_cents >= NOTABLE_BUY_MIN_CENTS:
                signals.append(_emit(t, "notable_buy", NOTABLE_BUY_SEVERITY,
                                     {"insider_count": 1, "total_value_cents": t.value_cents}))
```

`apps/web/emails/close-brief.tsx`, in `catalystLine`'s switch before the `default`:

```tsx
    case "notable_buy":
      return `Insider purchase — ${dollars(r.value_cents ?? 0, currencySymbol)}, ${on}${suffix}`;
```

Mirror in `apps/web/emails/cn/close-brief.tsx` only if it has its own `catalystLine` switch.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_catalysts.py tests/test_catalysts_assemble.py -v`
Expected: PASS. (If a snapshot-style assemble test now sees extra `notable_buy` rows from seeded data, that is the rule working — update the snapshot expectations.)

- [ ] **Step 5: Commit**

```bash
git add worker/constants.py worker/catalysts.py apps/web/emails tests/test_catalysts.py
git commit -m "feat(catalysts): notable_buy — any insider's >=\$100k open-market purchase"
```

---

### Task 6: the CLI ingests live when the key is set

**Files:**
- Modify: `worker/cli.py` (`_catalysts`, lines ~240-262)
- Test: `tests/test_cli.py` if a `_catalysts` wiring test exists (check `grep -n catalysts tests/test_cli.py`); otherwise this task's verification is the type check plus a by-hand run, matching how the existing provider wiring is covered

**Interfaces:**
- Consumes: `FdnCatalystProvider`, `FdnClient` (Task 3), `config.FDN_API_KEY`.
- Produces: `catalysts ingest` uses the live provider when the key is set, synthetic otherwise — the same switch the open job uses (`FdnClient() if config.FDN_API_KEY else None`).

- [ ] **Step 1: Implement**

In `worker/cli.py`'s `_catalysts`, replace the provider construction and the stale comment:

```python
            # Live when the key is set (M16's switch), synthetic otherwise —
            # the same rule the open job applies to the pre-market feed.
            if config.FDN_API_KEY:
                from worker.providers.fdn import FdnCatalystProvider, FdnClient

                client = FdnClient()
                try:
                    counts = ingest_catalysts(
                        conn, FdnCatalystProvider(client), symbols, as_of=session_date
                    )
                finally:
                    client.close()
            else:
                counts = ingest_catalysts(
                    conn, SyntheticCatalystProvider(session_date), symbols, as_of=session_date
                )
            print(f"catalysts ingest {session_date}: {counts}")
            return
```

Add `from worker import config` to the function's imports if the module doesn't already import it (check the top of `cli.py` — it does import `config` for other commands; reuse that).

- [ ] **Step 2: Type check and full test run**

Run: `uv run mypy worker/cli.py && uv run pytest tests/ -x -q`
Expected: clean, all pass.

- [ ] **Step 3: Commit**

```bash
git add worker/cli.py
git commit -m "feat(catalysts): CLI ingest goes live when FDN_API_KEY is set"
```

---

### Task 7: `fetch_week_news` — the close brief's week of held-name news

**Files:**
- Modify: `worker/news_fdn.py`
- Test: `tests/test_news_fdn.py`

**Interfaces:**
- Consumes: `FdnClient.fetch`, `FEED_ERRORS`.
- Produces: `fetch_week_news(client: FdnClient, *, session_date: date, held: set[str]) -> dict[str, list[str]]` — headlines newest-day-first, ≤ `_WEEK_PER_SYMBOL_CAP` (5) per symbol, over `_WEEK_DAYS` (7) calendar days ending at `session_date`. Task 9 calls it. `fetch_held_news` (the open path) is untouched.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_news_fdn.py`:

```python
def test_week_news_walks_seven_days_newest_first_and_caps_at_five() -> None:
    dates_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        d = request.url.params["date"]
        if request.url.params.get("offset", "0") != "0":
            return httpx.Response(200, text="[]")
        dates_seen.append(d)
        return httpx.Response(
            200, text=f'[{{"trading_symbols": ["ZHELD"], "article_headline": "h {d}"}}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert dates_seen == [
        "2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11",
        "2026-08-10", "2026-08-09", "2026-08-08",
    ]
    # Newest day first, capped at five — the freshest week survives the cap.
    assert got == {"ZHELD": [
        "h 2026-08-14", "h 2026-08-13", "h 2026-08-12", "h 2026-08-11", "h 2026-08-10",
    ]}


def test_week_news_pages_a_day_until_the_short_page() -> None:
    """A full page (10 records) means another page may follow; a short one ends
    the day. Live days ran ~20 pages deep (probe 2026-09-04)."""
    calls: list[tuple[str, str]] = []
    full = "[" + ",".join(
        '{"trading_symbols": [], "article_headline": "x"}' for _ in range(10)
    ) + "]"

    def handler(request: httpx.Request) -> httpx.Response:
        d, off = request.url.params["date"], request.url.params.get("offset", "0")
        calls.append((d, off))
        if d == _SESSION.isoformat() and off in ("0", "10"):
            return httpx.Response(200, text=full)
        return httpx.Response(
            200, text='[{"trading_symbols": ["ZHELD"], "article_headline": "deep"}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert (_SESSION.isoformat(), "20") in calls  # paged past two full pages
    assert "deep" in got["ZHELD"]


def test_week_news_a_bad_day_degrades_to_the_other_days() -> None:
    """One day 500ing (or 429ing) loses that day, not the week — the same
    contract as fetch_held_news, held per-day."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == _SESSION.isoformat():
            return httpx.Response(429)
        if request.url.params.get("offset", "0") != "0":
            return httpx.Response(200, text="[]")
        return httpx.Response(
            200, text='[{"trading_symbols": ["ZHELD"], "article_headline": "older"}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert got == {"ZHELD": ["older"] * 5}  # six good days, capped at five
```

Also set `news_fdn._THROTTLE_S = 0` at module scope in the test file (after the imports) so the suite doesn't sleep:

```python
from worker import news_fdn

news_fdn._THROTTLE_S = 0  # tests must not sleep between mock pages
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_news_fdn.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'fetch_week_news'` (import it in the test file's import block); existing four tests still pass.

- [ ] **Step 3: Implement**

In `worker/news_fdn.py`, extend the imports (`import time`, `from datetime import date, timedelta`) and add below `fetch_held_news`:

```python
_WEEK_DAYS = 7
_MAX_PAGES_PER_DAY = 30   # live days ran ~20 pages of 10 (probe 2026-09-04)
_WEEK_PER_SYMBOL_CAP = 5
_THROTTLE_S = 0.2         # paging flat-out drew 429s on the live key


def fetch_week_news(
    client: FdnClient, *, session_date: date, held: set[str]
) -> dict[str, list[str]]:
    """The close brief's news context (docs/04 rule 2, same as the open's):
    every held-name headline from the past ``_WEEK_DAYS`` calendar days,
    newest day first. ``latest-news`` has no per-symbol filter at any tier
    (Q10, probed 2026-09-04), so each day's market-wide feed is paged to
    exhaustion and filtered here. A day that fails mid-page keeps the pages it
    got and the loop moves on — one bad day loses a day, never the week.
    Headlines flow only to close narration; the redistribution surface stays
    zero (see the module header)."""
    out: dict[str, list[str]] = {}
    for back in range(_WEEK_DAYS):
        day = session_date - timedelta(days=back)
        try:
            for page in range(_MAX_PAGES_PER_DAY):
                records = client.fetch(
                    "latest-news", date=day.isoformat(), offset=str(page * 10)
                )
                if not records:
                    break
                for record in records:
                    headline = str(record.get("article_headline") or "").strip()
                    if not headline:
                        continue
                    for symbol in record.get("trading_symbols") or []:
                        if (
                            str(symbol) in held
                            and len(out.setdefault(str(symbol), [])) < _WEEK_PER_SYMBOL_CAP
                        ):
                            out[str(symbol)].append(headline)
                if len(records) < 10:
                    break
                time.sleep(_THROTTLE_S)
        except FEED_ERRORS:
            continue
    return out
```

Update the module docstring's first line to say the feed serves both the morning gate and the close brief's week window.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_news_fdn.py -v`
Expected: PASS, all seven.

- [ ] **Step 5: Commit**

```bash
git add worker/news_fdn.py tests/test_news_fdn.py
git commit -m "feat(news): fetch_week_news — seven days of held-name headlines"
```

---

### Task 8: close narration carries headlines

**Files:**
- Modify: `worker/narrate.py` (`build_prompt`, `narrate_and_apply`)
- Modify: `worker/assemble.py` (`assemble_and_store`)
- Test: `tests/test_narrate.py`, `tests/test_assemble.py`

**Interfaces:**
- Consumes: the open path's proven pattern — `build_open_prompt`'s `news_block` and `narrate_open_and_apply`'s pass-through (`worker/narrate.py:172-230`).
- Produces: `build_prompt(obj: BriefObject, headlines: dict[str, list[str]] | None = None) -> str`; `narrate_and_apply(obj, narrator, headlines: dict[str, list[str]] | None = None)`; `assemble_and_store(conn, user_id, session_date, kind="close", *, narrator=None, news: dict[str, list[str]] | None = None)`. Task 9 passes `news` through. All params default to `None` — every existing caller keeps working unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/test_narrate.py`, copy how an existing `build_prompt` test constructs its `BriefObject` fixture, then add:

```python
def test_close_prompt_folds_headlines_in_and_omits_the_block_without_them() -> None:
    """Same contract as the open prompt (M16): headlines are causal context,
    never a source of figures, and absence means no block at all."""
    obj = <the file's existing close-brief fixture>

    with_news = build_prompt(obj, {"ASTS": ["Insider buys disclosed", "Contract win"]})
    without = build_prompt(obj)

    assert "ASTS: Insider buys disclosed · Contract win" in with_news
    assert "News headlines for these names" in with_news
    assert "News headlines" not in without
```

In `tests/test_assemble.py`, find the existing `assemble_and_store` test that stubs a narrator, and add one asserting the pass-through — monkeypatch `worker.assemble.narrate_and_apply` with a recorder and assert it received `headlines={"ASTS": ["h"]}` when `assemble_and_store(..., news={"ASTS": ["h"]})` is called. Follow that file's existing fixture pattern for the DB-backed call.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_narrate.py tests/test_assemble.py -v -k headline`
Expected: FAIL — `build_prompt() takes 1 positional argument` / unexpected keyword `news`.

- [ ] **Step 3: Implement**

`worker/narrate.py` — `build_prompt` gains the parameter and the block (mirror `build_open_prompt` exactly; the close prompt's return already ends with framing + context):

```python
def build_prompt(obj: BriefObject, headlines: dict[str, list[str]] | None = None) -> str:
```

Inside, before the `return`, build `news_block` exactly as `build_open_prompt` does:

```python
    news_block = ""
    if headlines:
        lines = [f"  {sym}: " + " · ".join(hs) for sym, hs in sorted(headlines.items())]
        news_block = (
            "News headlines for these names from the past week (attribute moves "
            "to causes where they explain them; do not restate figures):\n"
            + "\n".join(lines) + "\n\n"
        )
```

and interpolate `f"{news_block}"` into the returned string just before the "Session data, for context only" line — the same position the open prompt uses.

`narrate_and_apply` passes it through:

```python
def narrate_and_apply(
    obj: BriefObject,
    narrator: Narrator | None,
    headlines: dict[str, list[str]] | None = None,
) -> BriefObject:
    """Stage ⑤. Return ``obj`` with prose merged in, or ``obj`` unchanged when
    narration is unavailable or fails. The broad ``except`` is the feature, not a
    hedge: a brief with no prose is useful; a brief that didn't send is not."""
    if narrator is None:
        return obj
    try:
        narration = parse_narration(narrator(build_prompt(obj, headlines)), obj)
    except Exception:
        return obj  # non-fatal (docs/02): tables-only, still valid and sendable
    return apply_narration(obj, narration)
```

`worker/assemble.py` — `assemble_and_store` gains `news: dict[str, list[str]] | None = None` in its keyword-only params, and the narration call becomes:

```python
    obj = narrate_and_apply(obj, narrator, headlines=news)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_narrate.py tests/test_assemble.py tests/test_assemble_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/narrate.py worker/assemble.py tests/test_narrate.py tests/test_assemble.py
git commit -m "feat(news): close narration folds in the week's held-name headlines"
```

---

### Task 9: the close job runs catalysts + news before assemble

This is the fix for the production miss: `run_session_job` gains a pre-assemble stage — ingest + detect + week news, live-keyed, non-fatal — completing the M17 design's "attaches to the existing PM stage, before the close assemble".

**Files:**
- Modify: `worker/catalysts.py` (move three helpers in from `cli.py`, made public)
- Modify: `worker/cli.py` (import the moved helpers; delete the private copies)
- Modify: `worker/scheduler.py` (`_pull_close_catalysts_and_news`, `run_session_job`)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `ingest_catalysts` (Task 4's hardened version), `rebuild_signals(conn, *, model_version, as_of, earnings, floats)`, `FdnCatalystProvider`/`FdnClient`/`store_captured_payloads`, `fetch_week_news` (Task 7), `book_symbols` (`worker/scheduler.py:365`), `assemble_and_store(..., news=...)` (Task 8).
- Produces: in `worker/catalysts.py` — `held_symbols(conn: Connection, user_id: str) -> list[str]`, `next_earnings(conn: Connection) -> dict[str, date]`, `book_floats(conn: Connection, session_date: date) -> dict[str, Decimal]` (bodies moved verbatim from `cli.py`'s `_book_symbols` / `_next_earnings` / `_book_floats`). In `worker/scheduler.py` — `_pull_close_catalysts_and_news(engine: Engine, user_id: str, session_date: date) -> dict[str, list[str]]`: returns `{}` and does nothing without `FDN_API_KEY`; never raises.

- [ ] **Step 1: Move the helpers**

Cut `_book_symbols`, `_next_earnings`, and `_book_floats` from `worker/cli.py` (bodies verbatim, docstrings included), paste into `worker/catalysts.py`'s "Database layer" section renamed `held_symbols`, `next_earnings`, `book_floats`, and update `cli.py`'s two call sites (`_catalysts` ingest uses `held_symbols(conn, DEV_USER_ID)`; detect/rebuild uses `book_floats(...)` / `next_earnings(...)`) with the import from `worker.catalysts`.

Run: `uv run pytest tests/ -x -q && uv run mypy worker/cli.py worker/catalysts.py`
Expected: everything passes — a pure move.

- [ ] **Step 2: Write the failing scheduler tests**

Add to `tests/test_scheduler.py` next to the other `run_session_job` tests, reusing `_stub_poll`, `_FakeEngine`/`_TxEngine`, and the ping monkeypatch pattern from `test_run_session_job_proceeds_when_bars_are_present` (line ~440):

```python
def test_close_job_pulls_catalysts_and_hands_news_to_assemble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The M17 wiring that was never built: the close run pulls the catalyst
    feeds and the week's news before assemble, and assemble gets the news."""
    monkeypatch.setattr(scheduler, "ping_success", lambda url: None)
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: None)
    _stub_poll(monkeypatch, missing=set())

    from worker import assemble, narrate

    monkeypatch.setattr(narrate, "default_narrator", lambda: None)
    seen: dict[str, Any] = {}

    def fake_assemble(conn, user_id, session_date, kind, *, narrator=None, news=None):
        seen["news"] = news
        return None  # quiet session — delivery never runs

    monkeypatch.setattr(assemble, "assemble_and_store", fake_assemble)
    monkeypatch.setattr(
        scheduler, "_pull_close_catalysts_and_news",
        lambda engine, user_id, session_date: {"ASTS": ["Insider buys disclosed"]},
    )

    outcome = scheduler.run_session_job(
        _TxEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 20, 45, tzinfo=UTC),
        healthcheck_url="",
    )

    assert outcome == "skipped-quiet"
    assert seen["news"] == {"ASTS": ["Insider buys disclosed"]}


def test_the_catalyst_stage_is_a_noop_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler.config, "FDN_API_KEY", "")
    assert scheduler._pull_close_catalysts_and_news(
        _FakeEngine(), "u", date(2026, 9, 4)
    ) == {}


def test_the_catalyst_stage_degrades_instead_of_killing_the_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor having a bad afternoon costs the catalysts section and the
    news block — never the brief (docs/02's non-fatal rule)."""
    monkeypatch.setattr(scheduler.config, "FDN_API_KEY", "k")

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("vendor down")

    import worker.providers.fdn as fdn

    monkeypatch.setattr(fdn, "FdnClient", _Boom)
    assert scheduler._pull_close_catalysts_and_news(
        _FakeEngine(), "u", date(2026, 9, 4)
    ) == {}
```

(`_TxEngine` currently lives inside `test_run_session_job_proceeds_when_bars_are_present` — hoist it to module level next to `_FakeEngine` so both tests share it, keeping its docstring.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -v -k "catalyst or news"`
Expected: FAIL — `scheduler._pull_close_catalysts_and_news` doesn't exist; `fake_assemble` never receives `news`.

- [ ] **Step 4: Implement**

In `worker/scheduler.py`, above `run_session_job`:

```python
def _pull_close_catalysts_and_news(
    engine: Engine, user_id: str, session_date: date
) -> dict[str, list[str]]:
    """The pre-assemble catalyst stage the M17 design scheduled and M17 never
    wired: pull the insider/144 feeds for the held names, rebuild signals, and
    fetch the week's held-name news. Live-keyed like every fdn surface; without
    the key it is a no-op and the close brief runs exactly as before.

    Never raises: each half degrades to a log line, because a vendor having a
    bad afternoon must cost the catalysts section and the news block, not the
    brief. Returns symbol -> headlines for close narration."""
    if not config.FDN_API_KEY:
        return {}

    from worker.catalysts import book_floats, held_symbols, next_earnings, rebuild_signals
    from worker.catalysts_ingest import ingest_catalysts
    from worker.constants import CATALYST_MODEL_VERSION
    from worker.news_fdn import fetch_week_news
    from worker.providers.fdn import FdnCatalystProvider, FdnClient, store_captured_payloads

    news: dict[str, list[str]] = {}
    try:
        client = FdnClient()
    except Exception as exc:  # noqa: BLE001 - degrade, never kill the close
        print(f"close {session_date}: catalyst stage skipped ({exc!r})")
        return news
    try:
        try:
            with engine.begin() as conn:
                held = held_symbols(conn, user_id)
                counts = ingest_catalysts(
                    conn, FdnCatalystProvider(client), held, as_of=session_date
                )
                stored = rebuild_signals(
                    conn, model_version=CATALYST_MODEL_VERSION, as_of=session_date,
                    earnings=next_earnings(conn), floats=book_floats(conn, session_date),
                )
            print(f"close {session_date}: catalysts {counts}, {stored} signals.")
        except Exception as exc:  # noqa: BLE001
            print(f"close {session_date}: catalyst ingest degraded ({exc!r})")
        try:
            with engine.connect() as conn:
                held_set = set(book_symbols(conn, user_id))
            news = fetch_week_news(client, session_date=session_date, held=held_set)
            with engine.begin() as conn:
                store_captured_payloads(conn, client, as_of=session_date)
            print(f"close {session_date}: week news for {sorted(news)}.")
        except Exception as exc:  # noqa: BLE001
            print(f"close {session_date}: news fetch degraded ({exc!r})")
    finally:
        client.close()
    return news
```

In `run_session_job`, between the bar-poll block and the assemble transaction, add:

```python
        # M17's scheduled stage, at last: catalysts + the week's news land
        # before assemble reads them. Non-fatal by construction.
        news = _pull_close_catalysts_and_news(engine, user_id, session_date)
```

and pass it through:

```python
                obj = assemble_and_store(
                    conn, user_id, session_date, "close",
                    narrator=default_narrator(), news=news,
                )
```

(Exception text printed here comes from our own code and SQLAlchemy, not from httpx — the httpx members of `FEED_ERRORS` are swallowed inside `fetch_week_news`/`ingest_catalysts` before they reach these prints. The one httpx escape path is `FdnClient()` construction, which raises our own keyless `RuntimeError`, and `rebuild_signals`, which does no I/O. This keeps `_safe_error`'s never-log-the-URL rule intact.)

- [ ] **Step 5: Run the scheduler tests, then everything**

Run: `uv run pytest tests/test_scheduler.py -v && uv run pytest tests/ -q && uv run ruff check && uv run mypy worker`
Expected: all pass, lint and types clean.

- [ ] **Step 6: Commit**

```bash
git add worker/scheduler.py worker/catalysts.py worker/cli.py tests/test_scheduler.py
git commit -m "feat(catalysts): the close job pulls catalysts and week news before assemble"
```

---

### Task 10: record the answers — Q10, the vendor shapes, the new rule

**Files:**
- Modify: `docs/open-questions.md` (move Q10 to Answered; note the vendor-shape finding under the Catalysts section)
- Modify: `docs/02-architecture.md` (the Company news source row)

**Steps:**

- [ ] **Step 1: Q10 → Answered**

Remove the open Q10 entry and add to the Answered section, matching its house style:

```markdown
### Q10 — Does `latest-news` honor a per-symbol filter, or is the news gate decorative?

**Answered 2026-09-04, probed live:** **IGNORED — no per-symbol filter exists
at any tier.** The vendor docs list only `date`, `offset`, `format`, and the
probe's `identifier` call returned 10 records, none mentioning the symbol.
Market-wide paging is the only shape: ~190 records / ~20 pages per day,
touching only ~27 distinct symbols — the feed skews to large caps, so a quiet
held name may genuinely have no rows. The close brief now pages the full prior
week (`fetch_week_news`, throttled — paging flat-out drew 429s even on the
Premium 30 req/s tier); the open gate keeps its 3-page morning skim.

*Residual guard:* Q10's original by-eye check still applies to the close job's
`close <date>: week news for [...]` log line — always-empty over a week of
sessions would now point at held names simply not clearing the feed's
large-cap skew, which is a data-coverage fact to record, not a bug to fix.
```

- [ ] **Step 2: Record the insider-feed shape finding**

Under the "Catalysts — FinancialData.net (M17/M18)" heading, add:

```markdown
### Vendor record shapes — probed 2026-09-04

`insider-transactions` and `proposed-sales` use field names disjoint from the
M17 synthetic guesses (`trading_symbol` not `symbol`, `amount_of_securities`
not `shares`, `relationship_to_issuer` not `insider_title`, codes already in
Form 4 letters, **no filing_date on either feed**). `normalize_*` and the
synthetic provider now speak the vendor shape; Form 144 rows are anchored on
`approximate_date_of_sale`, so `unconverted_144` measures "past the stated
sale date and still unexecuted". Q2's accepted false-positive cost is halved:
derivative legs are now skipped outright, though tax-withholding (`F`) rows
still count like sales in cluster math.
```

- [ ] **Step 3: Architecture table**

In `docs/02-architecture.md`, extend the Company news row's notes to add: "Close brief: `fetch_week_news` pages the prior 7 days market-wide (no per-symbol filter exists — Q10) and feeds held-name headlines to close narration only."

- [ ] **Step 4: Commit**

```bash
git add docs/open-questions.md docs/02-architecture.md
git commit -m "docs(catalysts): Q10 answered; live vendor shapes and the week news window"
```

---

### Task 11: live verification against production data

The unattended fire is tomorrow's proof; this is today's.

- [ ] **Step 1: Ingest + detect live for the book**

Run from `apps/worker` (requires the repo-root `.env`):

```bash
uv run python -m worker.cli catalysts ingest --date 2026-09-04
uv run python -m worker.cli catalysts rebuild --date 2026-09-04
```

Expected: nonzero insider counts; the rebuild stores signals including `notable_buy` for ASTS (ref_date 2026-08-31, Cisneros ≈ $500K) if ASTS is in the book's holdings.

- [ ] **Step 2: Dry-run the close brief**

Use the existing dry-run path (`scripts/dry_run_past_session.py` — check its `--help` for the date/kind flags) for 2026-09-04 and confirm the §5 catalysts section carries the ASTS insider purchase row and the narration context included week-news headlines (the job log's `week news for [...]` line).

- [ ] **Step 3: Report**

Paste the section rows and the log lines into the PR description; do not paste raw vendor bodies (licensing) or any URL containing `key=`.

---

## Self-review notes

- **Spec coverage:** miss-root-cause (no live provider, no scheduling, no matching rule) → Tasks 1-6, 9; the user's stated ask (week of news for the close) → Tasks 7-9; probe findings recorded → Task 10; live proof → Task 11.
- **Type consistency:** `fetch_week_news(client, *, session_date, held) -> dict[str, list[str]]` (T7) is what T9 calls; `assemble_and_store(..., news=...)` (T8) is what T9 passes; `held_symbols`/`next_earnings`/`book_floats` (T9 step 1) match T9 step 4's imports; `FdnCatalystProvider(client)` (T3) is constructed in T6 and T9.
- **Known accepted costs:** week-news coverage is bounded by the feed's large-cap skew (recorded in Q10's answer); `unconverted_144`'s clock semantics shift with the date anchor (recorded in code and docs); the $100K `notable_buy` threshold is a first calibration, expected to move like every constant in the §5 block.
