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
| `shares_out` | `EntityCommonStockSharesOutstanding` | `dei` | Cover-page count; falls back to `us-gaap:CommonStockSharesOutstanding`. See the coverage gap below |
| `public_float_cents` | `EntityPublicFloat` | `dei` | **A real public float, in USD.** Annual, from the 10-K cover page |
| `cash_cents` | `CashAndCashEquivalentsAtCarryingValue` | `us-gaap` | Instantaneous, not a duration |
| `quarterly_burn_cents` | `NetCashProvidedByUsedInOperatingActivities` | `us-gaap` | Stored **negated** — see "burn is not cash flow" below |
| `net_income_cents` | `NetIncomeLoss` | `us-gaap` | M19's `gaap_profitability` needs trailing-4Q *and* most-recent-quarter |
| `domicile` | `stateOfIncorporation` | *(submissions)* | **Not in companyfacts at all** — that endpoint carries numeric facts only, so a string fact needs `submissions` |

Each observation carries `val`, `form` (10-K / 10-Q), `fy`, `fp`, `end`,
`filed`, and `accn`. Duration concepts also carry `start`.

### Burn is not cash flow

`quarterly_burn_cents` stores **negated** operating cash flow. The column is a
burn and `flags.runway_quarters` divides by it, bailing out when the mean is
`<= 0`. Storing raw OCF inverts the flag exactly: a genuine cash-burner
(negative OCF) reports *no* runway, while a cash generator gets a meaningless
one. Confirmed against the live book before the fix — ASTS returned `None` and
SNDK returned 13 quarters, precisely backwards. After negation ASTS reports 33.4
quarters and SNDK correctly reports none.

### Coverage gap: multi-class registrants have no share count

`companyfacts` returns only facts **without dimensional qualifiers**. A company
reporting shares outstanding per share class tags them dimensionally, so no
total appears — verified live: ASTS exposes no `EntityCommonStockSharesOutstanding`
and no `us-gaap:CommonStockSharesOutstanding`, only per-class facts that the API
drops.

`shares_out` is therefore NULL for such registrants, and `dilution_yoy` stays
dormant for them. `WeightedAverageNumberOfSharesOutstandingBasic` *is* present
and non-dimensional, but it is a period average rather than a point-in-time
count — a different measure, deliberately **not** substituted. Silently swapping
one for the other is the class of quiet wrongness this repo's invariants exist
to prevent. The remedy, if it matters later, is the `companyconcept` endpoint or
the XBRL frames API, both of which expose dimensions; that is a separate piece
of work, not a footnote to this one.

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
    ADD COLUMN period_end         date NOT NULL DEFAULT '1900-01-01',
    ADD COLUMN fiscal_period      text,      -- Q1..Q4 | FY
    ADD COLUMN net_income_cents   bigint,    -- M19 gaap_profitability
    ADD COLUMN public_float_cents bigint,    -- M19 float; open question 4
    ADD COLUMN domicile           text,      -- M19 domicile, from submissions
    ADD COLUMN cik                text,
    ADD COLUMN source             text NOT NULL DEFAULT 'edgar';

ALTER TABLE fundamentals DROP CONSTRAINT fundamentals_pkey;
ALTER TABLE fundamentals ADD PRIMARY KEY (symbol, as_of, period_end);

CREATE INDEX fundamentals_period_idx ON fundamentals (symbol, period_end DESC);
```

**The primary key gains `period_end`, because `(symbol, as_of)` is not unique in
live data.** Apple's 2010-01-25 filing carries restated facts for two different
periods under two accession numbers, both with that filing date — found by
running the normalizer over a real 3.8MB response, not by reasoning about the
schema. The honest grain for a point-in-time store is "what we learned on this
date about this period."

One row per filing, still: facts from one accession are gathered together, since
two rows sharing a filing date would collide. That grouping has a wrinkle worth
knowing — a 10-Q's cover-page share count is dated *after* the quarter end its
financial statements cover, so `period_end` prefers the us-gaap facts, which
define the reporting period, and the cover count rides along.

An amendment filed later lands as a new row with the same `period_end`, and a
reader wanting the current view of a period takes the latest `as_of` for it —
restatements correct history going forward without rewriting it, the same
discipline `attribution.model_version` applies (D21).

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
- **M19 eligibility** — `gaap_profitability` (net income), `float`
  (`public_float_cents`), `market_cap` (`shares_out` × close from `bars_daily`)
  and `domicile` (from `submissions`) all gain inputs. With `liquidity` already
  computable from `bars_daily`, that is **5 of 5 criteria on real data** — with
  the caveat that `market_cap` inherits the multi-class `shares_out` gap above.

## As built: what the live API changed

Three things in this spec were corrected by running against the real endpoint
rather than reasoning from documentation, and they are recorded here because
each was wrong in a way that would have shipped:

1. **`domicile` is not in `companyfacts`.** That endpoint carries numeric facts
   only; the state of incorporation is a string and lives in `submissions`. The
   original draft named a `dei` concept that does not exist there.
2. **`EntityPublicFloat` does exist** — a real public float in USD, which the
   draft did not know about and which materially downgrades open question 4.
3. **The burn sign was backwards**, and the milestone's own DoD item asserted
   the wrong behaviour ("sign is preserved"). The live book exposed it: the
   cash-burner reported no runway and the cash generator reported one.
