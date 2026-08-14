# M18 — EDGAR fundamentals: filling the table three features already read

*Design spec. 2026-08-14. Blocks M19 (catalysts membership/eligibility).*

## Why this exists

`fundamentals` has held **zero rows since M7 created it**. The only code that
has ever written to it is `scripts/dry_run_flags.py`, a demo that rolls its
transaction back. `docs/02-architecture.md:132` has named the source since day
one — SEC EDGAR `companyfacts`, free, no key — and nothing was ever built.

This is the same shape of gap M14 found with `events` (D23-4) and M15 found with
`quotes` (D28): a table sketched, created for a feature, and never wired to a
source. It is worth stating plainly because the pattern has now recurred three
times, and each time it was found by the *next* milestone rather than by the one
that shipped the empty table.

**Three shipped or planned features read this table and get nothing:**

| Feature | Reads | Today |
|---|---|---|
| M7 `runway` flag | `cash_cents`, `quarterly_burn_cents` | never fires |
| M7 `dilution` flag | `shares_out` (vs a year ago) | never fires |
| M17 `large_144` | `shares_out` as a float proxy | always degrades to `standard_144` |
| M19 eligibility | `float`, `market_cap`, `gaap_profitability` | 3 of 5 criteria have no input |

The M7 flags are the open brief's §6 exposure check — the section M14 called
"finally the flags' intended home." It has been rendering position risk from an
empty table this whole time.

**This milestone costs nothing in licensing.** EDGAR is free, keyless, and
authoritative. It does not touch D8, it does not need FDN, and it does not wait
on any of the seven open questions.

## Scope

One ingest, one normalize, one migration. No BriefObject change, no renderer
change, no `schema_version` bump — every consumer already reads `fundamentals`
and will simply start getting rows.

Explicitly **not** in scope: revenue, margins, segment data, or anything that
would make this a general financials store. The columns are exactly what the
four consumers above need and nothing more.

## The API

```
https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
```

Four constraints that shape the design:

1. **Keyed by CIK, zero-padded to 10 digits — not by ticker.** The mapping comes
   from `https://www.sec.gov/files/company_tickers.json`, which is one small
   file covering every registrant. It is ingested once and cached; a symbol with
   no CIK is skipped, never guessed.
2. **A `User-Agent` with real contact info is mandatory.** SEC returns 403
   without it. This is a documented condition of use, not a nicety — it goes in
   config, not a literal.
3. **Rate limit is 10 requests/second**, enforced by the SEC across all its
   hosts. The universe here is a handful of symbols, so a simple limiter is
   enough; it is the same token-bucket shape M19 needs for `etf-holdings`.
4. **One response carries a company's entire XBRL history.** It is large (often
   several MB) and it is one call per symbol, not per period — so a full
   refresh is cheap in requests and expensive in bytes. Fetch weekly, not daily.

### The facts we take

`companyfacts` returns facts nested by taxonomy → concept → unit → a list of
period observations. The concepts that matter:

| Field | Concept | Taxonomy | Note |
|---|---|---|---|
| `shares_out` | `EntityCommonStockSharesOutstanding` | `dei` | Cover-page count; falls back to `us-gaap:CommonStockSharesOutstanding` |
| `cash_cents` | `CashAndCashEquivalentsAtCarryingValue` | `us-gaap` | Instantaneous, not a duration |
| `quarterly_burn_cents` | `NetCashProvidedByUsedInOperatingActivities` | `us-gaap` | Negative operating cash flow *is* the burn; sign is preserved and the flag interprets it |
| `net_income_cents` | `NetIncomeLoss` | `us-gaap` | **New column.** M19's `gaap_profitability` needs trailing-4Q *and* most-recent-quarter |
| `domicile` | `EntityIncorporationStateCountryCode` | `dei` | **New column.** M19's `domicile` criterion |

Each observation carries `val`, `form` (10-K / 10-Q), `fy`, `fp`, `end`,
`filed`, and `accn`. Duration concepts also carry `start`.

## The load-bearing decision: `as_of` is the **filed** date

An XBRL fact has two dates: the period it describes (`end`) and the date the
filing became public (`filed`). They differ by weeks or months.

**`fundamentals.as_of` must be `filed`, not `end`.** A fact about Q2 is not
knowable on the last day of Q2; it is knowable when the 10-Q is filed. Keying on
`end` would let a metric computed for session *D* read a number the market did
not have until *D + 45*.

This is not a theoretical concern in this repo. M11's DoD includes a **null test
— shuffled calendar → flat residuals — whose entire purpose is catching
look-ahead** (D21), and M12 excludes contaminated days from fits. Seeding
fundamentals keyed on period end would introduce exactly the leak those tests
exist to detect, into the one table that feeds position-risk flags.

Consequence: a quarter's figures appear in the table on the date they were
filed, and every read is "what was knowable as of session *D*" —
`WHERE as_of <= :session_date`, which is how `flags.py` and M17's `_book_floats`
already query it. No consumer changes.

The `end` date is kept as its own column so the trailing-4Q sum can group by
period rather than by filing (an amended 10-Q filed later describes the same
quarter).

## Data model

Migration **`0015_edgar_fundamentals`** extends the existing table rather than
adding a new one — four consumers already read `fundamentals` by name.

```sql
ALTER TABLE fundamentals
    ADD COLUMN period_end       date,      -- the period the facts describe
    ADD COLUMN fiscal_period    text,      -- Q1..Q4 | FY
    ADD COLUMN net_income_cents bigint,    -- M19 gaap_profitability
    ADD COLUMN domicile         text,      -- M19 domicile (ISO country / US state code)
    ADD COLUMN cik              text,
    ADD COLUMN source           text NOT NULL DEFAULT 'edgar';

CREATE INDEX fundamentals_period_idx ON fundamentals (symbol, period_end DESC);
```

`(symbol, as_of)` stays the primary key: one row per symbol per filing date. An
amendment filed later lands as a new row with the same `period_end`, and the
trailing-4Q reader takes the latest `as_of` per `period_end` — restatements
correct history going forward without rewriting it, the same discipline
`attribution.model_version` applies (D21).

`source` defaults to `'edgar'` so the synthetic rows `dry_run_flags.py` writes
stay distinguishable from ingested ones.

**Verbatim payloads to `raw_payloads`** (invariant 5, D13): `source='edgar'`,
`endpoint='companyfacts'`, `symbol`, `as_of` = fetch date, body = the response.
Normalize is a pure replay, so a concept-mapping bug is fixed by re-normalizing
rather than re-fetching several MB per symbol.

## Modules

```
worker/providers/edgar.py    EdgarClient — CIK map, companyfacts fetch, UA, rate limit
worker/fundamentals.py       normalize_companyfacts (pure) + ingest_fundamentals (DB)
```

`EdgarClient` speaks `httpx` directly, matching `TiingoProvider` and M16's
`FdnClient` — and for the same reason: `parse_float=Decimal` on every byte keeps
money off the float path. XBRL values arrive as JSON numbers, so this is the
only place the money invariant can be lost.

EDGAR does **not** go behind `MarketDataProvider`, `PremarketProvider`, or
`CatalystProvider`. It is a fundamentals source, not a market-data or catalyst
one, and D28's lesson is one protocol per capability rather than one protocol
widened until nothing satisfies it. A `FundamentalsProvider` protocol is
declared with the single method `company_facts(symbol)` so a future vendor swap
is a constructor change.

`normalize_companyfacts` is pure — a stored fixture in, typed rows out — which
is what makes the concept mapping testable without a network call, exactly as
`bars_from_payloads` is for M2.

## Validation (Definition of Done)

1. **CIK resolution** — a known ticker maps to its zero-padded CIK; an unknown
   ticker is skipped and reported, never guessed into a wrong company's filings.
2. **Point-in-time** — a fixture whose Q2 10-Q is filed in August produces a row
   with `as_of` in August and `period_end` in June. A read as of July returns
   **nothing** for that quarter. This is the test that would have caught the
   look-ahead, so it is written first.
3. **Restatement** — an amended filing for a period already stored lands as a
   new row and the trailing-4Q reader picks the later one; the original row is
   not mutated.
4. **Money** — every monetary field is integer cents via `Decimal`; a fixture
   with a fractional value round-trips exactly.
5. **Sign** — a company with negative operating cash flow yields a burn the
   `runway` flag reads correctly, and a profitable company does not produce a
   nonsense runway.
6. **Replay** — wiping `fundamentals` and re-normalizing from `raw_payloads`
   reproduces identical rows with **zero network calls** (the M2 property).
7. **Politeness** — no request goes out without the configured `User-Agent`, and
   the limiter holds at 10 req/s under concurrency. A 403 is reported as a
   configuration error, not retried into a ban.
8. **The dormant features wake up** — with the real book ingested, `runway` and
   `dilution` flags evaluate against real numbers, and M17's `large_144` can
   fire when a Form 144 clears 0.5% of shares outstanding.

**DoD:** `fundamentals` carries point-in-time rows for every symbol in the book,
sourced from EDGAR with no API key; M7's runway and dilution flags evaluate
against real filings for the first time; a replay from `raw_payloads` reproduces
the table byte for byte; and a read as of a date before a filing cannot see it.

## Out of scope

Revenue, margins, segments, or any financial statement line beyond the five
fields above · a general XBRL query layer · backfilling more than the trailing
eight quarters (enough for trailing-4Q plus a year-ago share count) ·
`next_earnings_date`, which stays with the calendar feed (M14/M16) rather than
moving to EDGAR · full-text filing retrieval · anything requiring a paid vendor.

## What this unblocks

- **M7 flags** (`runway`, `dilution`) — dormant since M7, live after this.
- **M17 `large_144`** — `pct_of_float` stops being universally null. Note
  `shares_out` is shares *outstanding*, an upper bound on float, so the rule
  stays conservative; open question 4 (a true float figure) is unchanged.
- **M19 eligibility** — `float`, `market_cap` (`shares_out` × close from
  `bars_daily`), and `gaap_profitability` all gain inputs. With `liquidity`
  already computable from `bars_daily`, that is **4 of 5 criteria on real data**;
  only `domicile` depends on the `dei` concept landing reliably.
