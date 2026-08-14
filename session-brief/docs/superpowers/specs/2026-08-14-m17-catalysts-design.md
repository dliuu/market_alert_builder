# M17/M18 — Catalysts: insider flow, index & ETF membership, lockups

*Design spec. 2026-08-14. Builds on M13 (attribution consumers) and M15 (the
provider-seam pattern). Covers two milestones — see "Milestone split" below.*

## What this adds

A **Catalysts section on the close brief**: for each owned and watched name, the
supply-and-membership events that move a stock for reasons the price series
cannot explain.

- Insider transactions (Form 4)
- Proposed insider sales (Form 144) — a *lead* indicator on supply
- Index membership changes, and **index eligibility** (the leading indicator)
- ETF membership and weight changes
- Lockup expiries

All of it from FinancialData.net through **M16's existing `FdnClient`** (httpx
direct, not the `fdnpy` SDK — see R5), behind a new `CatalystProvider` protocol.
**M16 must land first**: it supplies `FDN_API_KEY` and the client this milestone
extends. D8's redistribution question is untouched and stays a business call
(§"Licensing" below).

The payoff is not the section on its own. M13 ranks the close brief by
`|resid_z|` and leads `one_thing` with the largest idiosyncratic move (D24).
Today a big residual is an unexplained number. Catalysts is what turns
*"SNDK's residual was +3.1σ"* into *"…and three officers sold into it."* A
residual **with** a catalyst is an explanation; a residual **without** one is an
open question, and the brief should say which it is.

---

## How this spec differs from the source document

The source spec (`Session Brief — Catalysts Module v1.0`) was written against a
generic repo. Seven of its structural choices conflict with invariants or
existing code here. Each is reconciled below with a **Reverses if** clause, in
the style of `docs/07-decisions.md`, so none of this is a silent rewrite.

**Nothing about the detector rules (§5 of the source) changes.** The rules,
thresholds and severity semantics are adopted as written. What changes is where
the data lives, what protocol it arrives through, and how it reaches a reader.

### R1 — One `catalyst_signals` table, not six `sig_*` tables and a view

The source gives each source its own Tier-2 table, then immediately `UNION ALL`s
all six into `catalyst_feed` with a common shape:
`(source, source_id, ticker, ref_date, severity, kind, detail jsonb)`.

**The union view is the evidence that the single table is the right shape.**
Six tables plus a six-arm view, all of which must be kept in step with
`current_model_version()`, is three schema objects' worth of ceremony for a row
shape the design itself normalizes away before anything reads it.

This repo already has the precedent: `flags` is one table with a `flag_type`
CHECK and a `payload jsonb`, carrying nine heterogeneous flag types (docs/03).
`attribution` is likewise one wide shared table rather than one per factor.

So: **one `catalyst_signals` table**, shaped like the source's own feed view,
with `source` + `kind` CHECK-constrained and the per-source typed fields in
`detail jsonb`. `model_version` and the `member_ids` audit trail are kept
exactly as specified. `catalyst_feed` becomes a query in `worker/catalysts.py`,
not a database object.

Raw tables stay **separate** — they have genuinely different natural keys and
columns — and the detectors stay separate modules. Only the signal row is
unified, which is what the source says happens anyway ("unified only at render
time"), just one layer earlier.

*Reverses if:* a source needs to be queried on a typed column at a volume where
a `jsonb` expression index is measurably too slow — at a few hundred symbols it
will not be — or if a future source's signal row genuinely cannot be expressed
in the common shape.

### R2 — Shared tables, except reporting state, which is per user

Invariant 4 says every table carries `user_id`. `docs/03-data-model.md` carves
out the market-data tables: they are shared across the tenant base and keyed by
symbol, and `attribution` (D21) follows the same rule. That carve-out is what
makes ingest cost scale with the symbol universe rather than the user count
(docs/02).

Insider filings and index membership are facts about a symbol, not about a
book. So:

- `catalyst_insider_tx`, `catalyst_proposed_sales`, `catalyst_index_constituents`,
  `catalyst_etf_holdings`, `catalyst_ipos`, `catalyst_watermarks`,
  `catalyst_signals` — **no `user_id`**, keyed by symbol.
- `catalyst_reporting_state` — **carries `user_id`**, PK `(user_id, signal_id)`.

Report-once decay is a property of *a reader's* attention, not of the signal.
Two users holding SNDK must each get their own full → condensed → suppressed
curve; sharing one would mean the second user's brief silently opens at
"condensed" because the first user already read it.

### R3 — Raw payloads go to `raw_payloads`; normalize is a pure replay

Invariant 5: raw vendor payloads are stored verbatim in `raw_payloads` and never
mutated; recomputation replays from them. D13 states the same as a decision.

The source spec puts a `payload JSONB` column on each Tier-1 table. That
duplicates `raw_payloads` and creates a second, weaker verbatim store — weaker
because it is per-record rather than per-fetch, so a vendor field that appears
outside the mapped records is lost.

Instead the existing two-stage shape applies unchanged:

```
ingest    FdnClient → raw_payloads (source='fdn', endpoint=…, symbol, as_of, body)  verbatim
normalize raw_payloads → catalyst_insider_tx, catalyst_index_constituents, …        pure replay
detect    catalyst_* → catalyst_signals                                             pure
```

**M16 already established this exact path**, which is the strongest argument for
it: `FdnClient` captures `(endpoint, symbol, verbatim text)` on every successful
fetch and `store_captured_payloads` writes them with
`ON CONFLICT (source, endpoint, symbol, as_of) DO NOTHING`. Catalysts reuses
both rather than adding a second capture mechanism.

For the snapshot endpoints, `symbol` on `raw_payloads` is the **index or ETF
symbol** and `as_of` is the snapshot date, so that same unique key makes a
re-fetch idempotent for free.

This makes the source's headline guarantee strictly stronger. It asks that Tier
2 be rebuildable from Tier 1 with zero API calls; here **Tier 1 is rebuildable
too**, from `raw_payloads`, which is the property M2 already proves for bars
("a replay from `raw_payloads` reproduces it byte-for-byte").

The `payload jsonb` column is dropped from the typed tables. `natural_key`
stays — it is what dedups within a fetch.

### R4 — A third protocol, `CatalystProvider` — do not widen `MarketDataProvider`

M15 learned this the hard way (D24, D28): protocols are structural, and widening
the one shared protocol with premium-only methods broke `mypy --strict` because
`TiingoProvider` structurally cannot serve them (clean at the milestone's base,
6 errors after). `PremarketProvider` was split out for exactly this reason.

Catalysts is a third such feed. It gets a third protocol in
`worker/providers/base.py`:

```python
class CatalystProvider(Protocol):
    def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]: ...
    def proposed_sales(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]: ...
    def index_constituents(self, index_symbol: str) -> list[dict[str, Any]]: ...
    def etf_holdings(self, etf_symbol: str) -> list[dict[str, Any]]: ...
    def ipos(self) -> list[dict[str, Any]]: ...
    def income_statements(self, symbol: str) -> list[dict[str, Any]]: ...
    def market_cap(self, symbol: str) -> dict[str, Any]: ...
    def securities_information(self, symbol: str) -> dict[str, Any]: ...
```

`FdnProvider` implements it — it is already the class the M11/M14/M15 seams
name, and its constructor already takes `api_key` plus injectable fetchers, so
the shape is established. A `SyntheticCatalystProvider` mirrors
`SyntheticPremarketProvider`'s determinism contract (a pure function of
`(symbol, date)` via a hash, never `random`) so the section is developable and
snapshot-testable without burning quota.

### R5 — Extend M16's `FdnClient`; there is no `fdnpy` in the path to harden

The source spec §2.3 catalogues four `fdnpy` 0.5.0 defects and builds a wrapper
around them. **That wrapper is moot here, and for a better reason than any of
the four.** M16 (`feat/m16-fdn-live-feed`) already built `FdnClient` in
`worker/providers/fdn.py`, and it bypasses the SDK entirely:

> *Deliberately not the fdnpy SDK: fdnpy parses prices as float, and the money
> invariant wants `parse_float=Decimal` on every byte — the same reason
> TiingoProvider speaks httpx directly.*

The money invariant outranks SDK convenience, so catalysts speaks to the same
`FdnClient`. Against the source's four defects: the timeout is already handled
(`httpx.Client(timeout=30.0)`), and defects 2–4 describe `fdnpy` code that is
never executed. Do not reintroduce `fdnpy` as a dependency.

What M16's client genuinely does **not** have, and catalysts needs:

1. **Retry.** `fetch()` calls `raise_for_status()` and propagates. A 429 or a
   transient 500 fails the run. Needs an attempt cap (default 5), exponential
   backoff with a ceiling (default 60s) and jitter, retrying 429/5xx and never
   retrying other 4xx — structured logging, never `print`.
2. **Rate limiting.** No limiter exists. Standard is 10 req/sec, Premium 30. The
   ETF pass alone is order-300 calls (open question 5), which is the first
   workload here big enough to hit a ceiling. `worker/providers/rate_limit.py`,
   a token bucket, verified under concurrency.
3. **Pagination.** `fetch()` is single-shot; catalysts needs bounded walks:
   - `fetch_page(endpoint, params, offset)` — one page.
   - `fetch_until(endpoint, params, predicate)` — walks while
     `predicate(record)` holds. **Never drains history.**

The pagination point survives the SDK's removal intact, because it is a property
of the *API*, not the SDK: there are **no date-range parameters on any
endpoint** and records return newest-first, so bounded recency is `offset=0`
walking forward while records stay newer than the watermark. Without
`fetch_until`, the only way to ask for recent insider filings is to read every
filing the issuer has ever made.

Page sizes: insider-transactions 50, proposed-sales 100, index-constituents 300,
etf-holdings 50, income-statements 50; ipos / market-cap /
securities-information unpaginated. *VERIFY these against the live API* — they
came from reading `fdnpy`'s source, which is no longer the code making the
request.

**Dependency:** M16 must land before this milestone starts, or `FdnClient` and
`FDN_API_KEY` do not exist to extend. All three additions above are extensions
to M16's file, so they are also the most likely merge conflict — sequence
accordingly.

### R6 — Thresholds in `worker/constants.py`; severity maps to `tier`

The source puts severity definitions in `config/severity.yaml`. **This repo has
no YAML config layer** — M15 put `TAPE_SYMBOLS`, `LEVEL_QUOTED` and
`FOREIGN_PROXIES` in `worker/constants.py`, and that is the precedent. A new
`# --- Catalysts (M17/M18) ---` block goes there. The source's real requirement
is *"thresholds in one place, tunable without touching detector code"*, and a
constants module satisfies it; introducing a YAML loader, a schema validator and
a config-precedence story to satisfy the letter of it is the kind of speculative
machinery §"Conventions" below rules out.

Severity 1–5 is **internal to the module**. It never reaches the BriefObject as
a number, because D16 is explicit that the renderer never decides what to hide —
assembly does, via `tier`. The map, applied in `assemble.py`:

| Severity | Meaning | `tier` |
|---|---|---|
| 5 | Act tonight | `full` |
| 4 | Read this | `full` |
| 3 | Worth knowing | `brief` |
| 2 | Context | `brief` |
| 1 | Archival | `suppressed` (queryable, never rendered) |

Report-once decay (§"Rendering") demotes the tier on repeat sightings; it does
not mutate the stored severity.

*Reverses if:* threshold tuning turns out to need edits between deploys, at
which point a YAML file with a Pydantic schema is a contained change — the
constants module is already the single import site.

### R7 — `catalyst_reporting_state` alongside `flags.last_seen`, not instead of it

D18 established that rate limiting belongs in the data, not the renderer, via
`flags.last_seen` — "otherwise you can't answer *when did I last warn myself
about this?*". Catalysts needs the same principle with a richer shape:
`last_seen` expresses a cooldown, but not a **decay curve** (full → condensed →
suppressed) and not **re-escalation** on a severity increase.

So a separate table, honouring the same principle. The load-bearing property,
carried over verbatim from the source:

> **`catalyst_reporting_state` must never be dropped when rebuilding signals.**

If report-once state were a column on `catalyst_signals`, a rebuild would wipe it
and every stale cluster would resurface at full volume on the next brief. The
rebuild path (`catalysts rebuild`) drops and repopulates `catalyst_signals`
only, and an explicit regression test asserts reporting state survives it.

Because a rebuild reassigns `catalyst_signals.id`, reporting state cannot key on
it. It keys on the **stable natural identity** of a signal:
`(user_id, source, symbol, kind, ref_date)`. This is a correction to the source
spec, which keys on `(source, source_id)` — a surrogate key that its own rebuild
story invalidates.

---

## Data model

Migration **`0014_catalysts`** (M17) and **`0015_catalyst_membership`** (M18).
Note `0010` is already doubled in `alembic/versions/` — `0014` is the next free
number, not the next after a count.

### M17 tables

```sql
catalyst_insider_tx (
  id BIGSERIAL PK, symbol, insider_name, insider_title,
  transaction_date DATE, filing_date DATE, transaction_type,
  shares NUMERIC, price_cents BIGINT, value_cents BIGINT, shares_after NUMERIC,
  natural_key TEXT UNIQUE, ingested_at TIMESTAMPTZ
)                                     -- INDEX (symbol, filing_date DESC)
                                      --       (symbol, transaction_date DESC)

catalyst_proposed_sales (
  id BIGSERIAL PK, symbol, insider_name, filing_date DATE,
  shares_proposed NUMERIC, approx_sale_date DATE, broker,
  natural_key TEXT UNIQUE, ingested_at TIMESTAMPTZ
)                                     -- INDEX (symbol, filing_date DESC)

catalyst_watermarks (
  source TEXT, symbol TEXT, last_success_at TIMESTAMPTZ, last_seen_date DATE,
  seen_keys TEXT[], last_error TEXT, consecutive_fails INT DEFAULT 0,
  PK (source, symbol)
)

catalyst_signals (
  id BIGSERIAL PK,
  source TEXT NOT NULL,          -- insider | proposed | index | etf | lockup | eligibility
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,            -- the signal_type / change_type / criterion
  ref_date DATE NOT NULL,
  severity SMALLINT NOT NULL,    -- 1..5
  detail JSONB NOT NULL,         -- typed per-source fields, see below
  member_ids BIGINT[] NOT NULL,  -- audit trail into the raw tables
  model_version TEXT NOT NULL,
  computed_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (source, symbol, kind, ref_date, model_version)
)                                -- INDEX (symbol, ref_date DESC)

catalyst_reporting_state (
  user_id UUID NOT NULL, source TEXT, symbol TEXT, kind TEXT, ref_date DATE,
  first_reported_at TIMESTAMPTZ, last_reported_at TIMESTAMPTZ,
  report_count INT DEFAULT 1, max_severity_seen SMALLINT,
  PK (user_id, source, symbol, kind, ref_date)
)
```

`detail` contents by source, so the renderer knows what it may read:

| `source` | `detail` keys |
|---|---|
| `insider` | `insider_count`, `total_value_cents`, `pct_of_holding`, `days_to_event` |
| `proposed` | `shares`, `pct_of_float` |
| `index` | `index_symbol`, `effective_date`, `prior_weight`, `new_weight` |
| `etf` | `etf_symbol`, `prior_weight`, `new_weight`, `aggregate_etf_pct` |
| `lockup` | `listing_date`, `expiry_date`, `shares_releasable`, `pct_of_float`, `date_source` |
| `eligibility` | `criterion`, `status`, `value`, `threshold`, `changed_from_prior` |

### M18 tables

```sql
catalyst_index_constituents (index_symbol, snapshot_date, symbol, weight,
                             ingested_at, PK (index_symbol, snapshot_date, symbol))
catalyst_etf_holdings       (etf_symbol, snapshot_date, symbol, weight, shares,
                             ingested_at, PK (etf_symbol, snapshot_date, symbol))
                            -- INDEX (symbol, snapshot_date DESC)  ← the reverse index
catalyst_ipos               (symbol PK, listing_date DATE, ingested_at)
```

**Money is integer cents** (`price_cents`, `value_cents`, `total_value_cents`),
`Decimal` in Python, never float — the source's `NUMERIC value_usd` violates a
standing invariant. Share counts and weights stay `NUMERIC`: they are quantities
and ratios, not money.

**Retention, no partitioning.** The source asks for monthly partitions on the
two snapshot tables. Six indices × ~2,500 names × 252 sessions is ~3.8M rows a
year — real, but well inside what Postgres 16 handles unpartitioned, and
partition management is a standing operational cost this repo has nowhere else.
Ship the retention job the source specifies (prune snapshot dailies older than
90 days, keep month-ends indefinitely) and revisit partitioning if the table
passes ~10M rows. The retention job is a `DELETE`; partitioning is a schema
regime.

### `index_events` — already there, and empty

`index_events` exists (migration `0010_attribution_econometrics`) and is read by
**two** existing modules: `exclusions.contaminated_days()` for M12's
contaminated-day fit mask, and `concordance.py` for M13's event-concordance
check (D26). Both docs describe it as "empty until curated."

**M18's index differ is what curates it.** Every `source='index'` signal also
writes `(symbol, trade_date)` to `index_events`. This is the highest-leverage
integration in the milestone and costs one insert: index-reconstitution days
become real fit-exclusions, so a reconstitution-driven move stops contaminating
β, and `concordance.py` gains genuine event mass instead of running against
earnings alone.

Constraint: it must not disturb M12's fixtures, which plant their own
`index_events` rows. The write is additive and `ON CONFLICT DO NOTHING`.

---

## Detector rules

Adopted from the source spec §5 unchanged. Restated here in repo terms only
where a rule touches an invariant.

### Insider (`source='insider'`)

| `kind` | Rule | Severity |
|---|---|---|
| `clevel_buy` | Open-market **purchase** by CEO/CFO/President | 5 |
| `cluster` | ≥3 distinct insiders, same direction, within 5 **trading** days | 4 |
| `pre_earnings` | Sale within 5 trading days before a known earnings date | 4 |
| `outsized_sale` | Single sale ≥40% of that insider's pre-transaction holding | 3 |
| `cadence_break` | Insider with ≥4 prior filings breaks rhythm, or sells ≥2× typical size | 3 |

"Within 5 trading days" uses `worker/calendar.py` / `exchange_calendars`
(invariant 7). Never `timedelta(days=5)`.

`days_to_event` reads the existing `events` table (`event_type='earnings'`),
which M14 already populates. If absent, `days_to_event` is NULL and
`pre_earnings` is skipped — never inferred.

**Suppression:** tax-withholding dispositions and option exercises at grant
price. *VERIFY* (open question 2) whether `transaction_type` distinguishes them.
If it does not: do **not** silently include them and do **not** silently drop
them — reduce severity by 1 and annotate. Guessing in either direction is worse
than the ambiguity.

Multiple types on one filing set emit as separate rows; assembly collapses per
symbol and takes the max severity.

### Proposed sales, Form 144 (`source='proposed'`)

Form 144s are filed *before* execution, so this is the one genuinely
forward-looking supply signal in the module. Frame the output that way.

| `kind` | Rule | Severity |
|---|---|---|
| `large_144` | Proposed shares ≥0.5% of float | 4 |
| `standard_144` | Any new 144 | 2 |
| `unconverted_144` | 144 filed >45 days ago with no matching Form 4 | 3 |

`unconverted_144` joins back to `catalyst_insider_tx` on
`(symbol, insider_name)` within a tolerance window. Track conversion lag per
symbol — it is cheap here and nobody else computes it.

### Index membership (`source='index'`, M18)

Nightly snapshot of `^GSPC`, S&P 400, S&P 600, Russell 1000, 2000, 3000. Diff
`snapshot_date = T` against the most recent prior snapshot.

Severity: S&P 500 addition/deletion 5; other tracked indices 4; weight shift
beyond 25% relative 2.

Three cases the differ must handle explicitly, because each is a plausible
silent-corruption bug:

- **First run** — no prior snapshot means *no events*, not "everything added."
- **Missed day** — diff against the most recent available snapshot and record
  the gap; do not assume yesterday.
- **Partial pull** — a truncated constituent list looks exactly like a mass
  deletion. Reject a snapshot whose size differs from the prior by more than a
  configured fraction, and record it as a failed pull.

The third is not in the source spec and is the failure mode most likely to
produce a brief claiming your entire book left the S&P 500.

*VERIFY, high priority* (open question 1): whether `index-constituents` reflects
**announcement** or **effective** membership. S&P announces after the close,
typically Friday, effective roughly a week later. If the endpoint is
effective-only, the diff detects changes ~5 trading days late and the signal is
lagging rather than leading — which changes what the section is *for*. This must
be answered before M18 ships.

### ETF holdings (`source='etf'`, M18)

20–40 relevant ETFs (SMH, SOXX, ARKX, ARKQ, BOTZ, ROBO, XLK, IGV, plus themes
matching the book's sectors — the same book-relevance principle as M15's
`FOREIGN_PROXIES`, which keys foreign proxies off the sector benchmark the book
already stores).

Weekly, staggered across weekdays to keep it off the nightly critical path.
`etf-holdings` pages at 50: SPY alone is ~10 calls, 30 ETFs is order-300 calls.

| `kind` | Rule | Severity |
|---|---|---|
| `addition` | Symbol newly present in an ETF | 3 |
| `deletion` | Symbol no longer present | 3 |
| `weight_shift` | Weight change ≥30% relative | 2 |

Also maintain `aggregate_etf_pct` — total ETF ownership as a share of the
symbol — as a mechanical-demand measure. The reverse index
`(symbol, snapshot_date DESC)` makes "which ETFs hold X, at what weight" one
indexed lookup.

### Lockups (`source='lockup'`, M18)

**Zero API polling.** Derived from `catalyst_ipos.listing_date`:

```
expiry_date = listing_date + 180 calendar days   →  snapped forward to the next trading day
```

180 days is a **convention, not a fact** — de-SPACs and staggered releases
routinely differ. Therefore `date_source = 'assumed_180d'` by default, a
per-symbol override table permitting `'confirmed'` / `'override'`, and assumed
dates **must render with visible uncertainty**: `[assumed 180d]`.

Note the deliberate mix: the 180 days is *calendar* (the convention is written
in calendar days), but the T-30 / T-5 / T-0 alert dates are *trading* days off a
trading-day-snapped expiry. Getting this backwards is an easy, silent
off-by-a-few-days.

Fire at T-30 (severity 2), T-5 (3), T-0 (4). `pct_of_float` from
`securities-information` / `market-cap` where derivable; NULL renders as "size
unknown" rather than being omitted — an unknown size is information, a missing
row is not.

### Index eligibility (`source='eligibility'`, M18)

**The highest-value component.** Everything else in this module reports what
happened; this one reports what is *about to become possible*. Recompute daily,
emit only on transition.

| `criterion` | Source | Threshold |
|---|---|---|
| `gaap_profitability` | `income-statements` — trailing 4Q GAAP net income > 0 **and** most recent quarter > 0 | > 0 |
| `market_cap` | `market-cap` | configurable, current S&P threshold |
| `float` | `securities-information` | ≥ 10% public float |
| `liquidity` | `bars_daily` — volume × price, trailing 6 months | configurable |
| `domicile` | `securities-information` | US |

`liquidity` reads **`bars_daily`**, which this repo already fills nightly — no
API call, and it is the one criterion the existing pipeline can answer for free.

Store all criteria every day, even unchanged ones; the value is the transition.
Severity 4 when an unmet criterion becomes met, 3 for the reverse.

Missing inputs yield `status='unknown'`, **never `not_met`**. Inferring "fails
the test" from "we have no data" is how this feature would produce its most
embarrassing output.

Discretionary criteria — committee judgment, share-class structure, Up-C
structures — are **out of scope and must not be presented as mechanically
determinable**. A standing footnote renders with the section: *"mechanical
criteria only; committee discretion not modelled."*

---

## Rendering — through the BriefObject, like everything else

This is the largest gap in the source spec: its §7 renders text directly from a
database view. Nothing in this repo reaches a reader that way. The pipeline is
`assemble → BriefObject → { web page, email HTML, plaintext }` (docs/04), and
the object is the contract.

### Object shape

A new `catalysts` section id, on the **close** brief (this is post-close data).
`schema_version` **5 → 6**, old renderers kept (docs/04 rule).

Section rows, one per symbol with catalysts, ordered by max severity descending:

```jsonc
{
  "id": "catalysts",
  "tier": "full",
  "rows": [
    { "symbol": "SNDK", "tier": "full",
      "items": [
        { "source": "insider", "kind": "cluster", "ref_date": "2026-08-13",
          "insider_count": 3, "value_cents": 1420000000, "pct_of_holding": null,
          "why": null },
        { "source": "eligibility", "kind": "gaap_profitability",
          "ref_date": "2026-08-14", "status": "met", "changed_from_prior": true,
          "why": null }
      ] }
  ],
  "note": "mechanical criteria only; committee discretion not modelled"
}
```

`why` is narrated per item and is subject to the **digit guard** (D19, docs/04):
the model writes causal prose, every figure is substituted from the object, and
an item whose narration contains a digit renders without prose rather than with
a hallucinated number.

`pnpm contracts:gen` regenerates the Pydantic and TS bindings; CI fails if
dirty. `apps/worker/tests/test_contract_schema.py` validates stored fixtures
against `brief-object.schema.json` directly — the note in docs/04 about codegen
happily emitting a `null` the schema rejects applies to every nullable field
above.

### Report-once decay

The renderer joins the feed to `catalyst_reporting_state`:

| `report_count` | Treatment |
|---|---|
| 1st | Full volume |
| 2nd | Condensed to one line (`tier: brief`) |
| 3rd+ | Suppressed |

**Re-escalation:** if current severity exceeds `max_severity_seen`, reset to
full. A 2-insider cluster becoming a 4-insider cluster is new information.

Symbols with no catalysts are suppressed into a single trailing roll-up line —
the same suppression principle as M5 and M15 §3, and the same reasoning:
explicit absence is information and is cheap. Stale sources are always named.

```
CATALYSTS

⚠ SNDK
   Insider cluster — 3 officers, $14.2M, 08/09–08/13
   CFO sold 42% of holding (largest in 3y) · 2 days pre-earnings
   Index: profitability criterion now met — 3 of 4 mechanical
   blockers cleared (float, liquidity, profitability)

  RKLB
   Form 144 — 1.2M shares proposed (0.24% float)
   Lockup T-30 (09/13): 8.4M shares, ~1.7% float [assumed 180d]

  No catalysts: ASTS, SOFI, AAOI, INFQ
  ETF data: last updated 08/07
```

Both renderers: the React Email close template and the web archive.
`design/design-reference.html` is the visual authority — open it before touching
either (CLAUDE.md).

### Claims — two reserved types finally get a source

`claims` has carried `catalyst_pending` and `supply_overhang` in its
`claim_type` CHECK since migration `0006`, deferred because "they need data that
lands at M7" (D17, docs/07). This is that data. **No migration is needed for the
claim types** — but note M15's lesson (D28): the contract enum and the database
CHECK are two separate sources of truth, so verify both before assuming either.

- `supply_overhang` — a large 144 or a T-5 lockup predicts relative weakness.
- `catalyst_pending` — an eligibility transition predicts relative strength.

Both grade under D24's rule: **against the realized residual, not raw relative
return**. Beta earns no credit — if a name outperformed because the market rose,
the claim was not right. `graded_model_version` stamps the attribution model
version used.

### The attribution pairing — why this milestone exists

`worker/catalysts.py` exposes:

```python
def catalysts_for(conn, symbol: str, on: date) -> list[CatalystItem]: ...
```

M13 already ranks attribution rows by `|resid_z|` and leads `one_thing` with the
top residual (D24). D27 documented cross-module trigger seams as *documented,
not built*. This is the first one built. Assembly pairs a residual-material row
(`|resid_z| >= 2.0`, `attribution.RESID_MATERIAL_Z`) with same-day catalysts and
the brief distinguishes the two cases:

- residual **+** catalyst → an explanation.
- residual **−** catalyst → an open question, and it says so.

The second is the more valuable output and the easier one to accidentally drop.

---

## Scheduling

No new orchestrator. The source's `orchestration/pm_run.py` is this repo's
existing scheduler (D20: a daily heartbeat that self-reschedules off the real
close and pings Healthchecks on every run), which M13 already extended with an
18:30 PM attribution score.

Catalysts ingest attaches to the **existing 18:30 PM stage**, before the close
assemble. Per source:

| Job | Cadence | Stage |
|---|---|---|
| `catalysts ingest --source insider,proposed` | daily | 18:30 |
| `catalysts snapshot --indices` | daily | 18:30 |
| `catalysts snapshot --etfs` | weekly, staggered | 18:30 |
| `catalysts ingest --source ipos` | weekly | 18:30 |
| `catalysts detect` | daily | 18:35 |
| `catalysts eligibility` | daily | 18:35 |

CLI subcommands under `worker/cli.py`, mirroring how `attribution refit` /
`score` / `concordance` are wired — scheduler-ready but independently runnable,
which is what made M11's walking skeleton testable.

**Independent failure domains.** Each ingest and each detector fails alone. A
failed ETF pull degrades one line and surfaces as `"ETF data: last updated
08/07"`, it does not kill the section. `consecutive_fails >= 3` on any source
alerts. A Healthchecks check per source, per D20's dead-man's-switch pattern.

**Idempotency.** Re-running the PM job for a trade date is a no-op, not a
duplicate — enforced at three layers: `raw_payloads`' unique key, the raw tables'
`natural_key`, and `catalyst_signals`' `UNIQUE (source, symbol, kind, ref_date,
model_version)`.

---

## Milestone split

The source is ten work packages — comparable in size to attribution, which this
repo split across M11/M12/M13. Same treatment, cut so that **M17 ships something
a reader sees**.

### M17 — Catalysts: insider flow and the brief section

The hardened client, the event-stream sources, their detectors, and the whole
render path. WP-0 (partial), WP-1, WP-2, WP-6, WP-8, WP-9 (partial).

- `0014_catalysts` — insider, proposed, watermarks, signals, reporting state
- `worker/providers/fdn_client.py`, `rate_limit.py` — all four defects fixed
- `CatalystProvider` protocol + `FdnProvider` + `SyntheticCatalystProvider`
- `worker/catalysts_ingest.py`, `worker/catalysts_detect.py`, `worker/catalysts.py`
- `schema_version` 6, the `catalysts` section, both renderers, `contracts:gen`
- Report-once decay + re-escalation, PM-stage scheduling

### M18 — Catalysts: membership, lockups, eligibility

The state-and-calendar sources. WP-3, WP-4, WP-5, WP-7, remainder of WP-9.

- `0015_catalyst_membership` — constituents, ETF holdings, IPOs
- Index and ETF snapshot + diff, `index_events` curation
- Lockup calendar + overrides
- The eligibility engine
- Retention job

Dependency shape within each:

```
0014 ─ fdn_client ─┬─ insider ingest ── insider detect ─┬─ section ── schedule   M17
                   └─ proposed ingest ─ proposed detect ┘

0015 ──────────────┬─ index snapshot ── index diff ── index_events              M18
                   ├─ etf snapshot ──── etf diff
                   ├─ ipo ingest ────── lockup calendar
                   └─ eligibility engine
```

---

## Validation (Definition of Done)

### M17

1. **Hardened client** — unit tests cover timeout, connection error (proving no
   `UnboundLocalError`), 429 with retry, 500 with retry, non-retryable 4xx, and
   retry exhaustion. Rate limiter verified under concurrency. No test makes a
   live call.
2. **No history drain** — a regression test asserts `fetch_until` stops at the
   watermark, by call count, not by result length.
3. **Idempotent ingest** — running twice inserts zero duplicate rows; a
   *backdated* filing injected into fixtures is still detected as new (the
   watermark alone would miss it; the rolling 30-day `seen_keys` set is what
   catches it).
4. **Golden detectors** — a hand-built fixture per `kind`, asserting exact
   signal output. A threshold change in `constants.py` alters output with no
   code change.
5. **Rebuild is free and safe** — `catalysts rebuild` reproduces identical
   signals from the raw tables with **zero API calls**, and an explicit
   regression test asserts `catalyst_reporting_state` survives it.
6. **Decay** — a signal reported three consecutive sessions renders full →
   condensed → suppressed; a severity increase on day 3 re-escalates to full.
7. **Object** — the section renders in Gmail *and* Outlook under 80KB and on the
   web archive; old renderers intact; `pnpm contracts:gen` clean; an M15-era
   brief still renders under `schema_version` 5.
8. **Empty is not blank** — an all-quiet session renders the roll-up line, not
   an empty section header.

**M17 DoD:** the close brief carries a Catalysts section driven by real Form 4 /
Form 144 flow, decaying on repeat and re-escalating on severity, rebuildable
from stored payloads without an API call, and it pairs a residual-material name
with its catalyst — or says there isn't one.

### M18

1. **Diff correctness** — a synthetic fixture with one known addition and one
   deletion produces exactly two events; a first run against an empty table
   produces **zero**; a missed day diffs against the most recent available and
   records the gap; a truncated snapshot is rejected rather than read as mass
   deletion.
2. **`index_events` curated** — an index signal writes `index_events`, and
   `exclusions.contaminated_days()` picks it up as a fit-exclusion without
   disturbing M12's planted fixtures.
3. **Reverse lookup** — "which ETFs hold X" returns the correct set and weights;
   per-ETF call count matches expected pagination; the stagger holds across a
   simulated week.
4. **Lockups** — an override supersedes the 180-day assumption; a symbol at
   exactly T-30 / T-5 / T-0 produces one event each, not duplicates; assumed
   dates carry `date_source='assumed_180d'` and render `[assumed 180d]`.
5. **Eligibility** — a symbol crossing into trailing-4Q GAAP profitability emits
   exactly one `changed_from_prior` event, on the crossing date; missing
   fundamentals yield `unknown`, never a false `not_met`; the discretionary
   footnote is present.
6. **Degradation** — killing any single ingest mid-run still produces a brief
   with the remaining sources and names the stale one.

**M18 DoD:** membership, lockup and eligibility catalysts land in the same
section, index reconstitution days feed attribution's exclusion mask, and an
eligibility transition emits a `catalyst_pending` claim the close brief later
grades against the realized residual.

---

## Open questions

Tracked in `docs/open-questions.md`. Each needs a live key to answer, and each
blocks a specific package — none of them block starting.

| # | Question | Blocks | If unresolved |
|---|---|---|---|
| 1 | Does `index-constituents` reflect announcement or effective membership? | M18 index diff | Changes detected ~5 trading days late; may need a news-based supplement |
| 2 | Does `insider-transactions` distinguish tax-withholding and option exercises? | M17 detectors | False positives on `outsized_sale` / `cluster`; severity −1 and annotate |
| 3 | Does `proposed-sales` expose a stable filing identifier? | M17 ingest | Falls back to a composite hash; slightly weaker dedup |
| 4 | Does `securities-information` provide float reliably? | M18 lockups, M17 `large_144` | Size context renders "unknown" |
| 5 | Actual `etf-holdings` call volume for the tracked set | M18 ETF | Rate-limit budget unknown |
| 6 | Does `initial-public-offerings` cover de-SPACs and direct listings? | M18 lockups | Silent gaps in the lockup calendar |
| 7 | Does `index-constituents` cover the Russell family at all? | M18 index diff | FTSE Russell licenses constituent data separately from S&P; three of the six tracked indices may simply not be available |

Question 7 is not in the source spec and should be checked **first** — it is one
API call, and it determines whether half the index work exists.

---

## Licensing

The API key lands with a prior push, so *access* is settled. What is not settled,
and is unchanged by this milestone:

- Insider transactions, proposed sales and ETF holdings need **Premium**;
  index-constituents needs **Standard**. The free tier (300 req/day) cannot run
  this module at all.
- FinancialData.net's Free/Standard/Premium tiers are licensed **personal use
  only**. Commercial use — i.e. the moment there is a user #2 paying — requires
  the Professional/Enterprise tier.

This is exactly D8's shape and stays a business decision, not a code blocker.
Nothing in this design assumes redistribution rights; the section works for a
book of one today, and the seam means the vendor can change without touching a
detector.

---

## Conventions

Existing repo conventions apply and are not restated, with three worth calling
out because the source spec names them differently:

- **`mypy --strict`, `ruff`, `pytest`** via `uv`. The source's Python 3.12 and
  strict-typing requirements match what is already enforced.
- **Parameterized queries only.** No string interpolation into SQL.
- **`@pytest.mark.integration` is not currently a registered marker** —
  `[tool.pytest.ini_options]` in `apps/worker/pyproject.toml` sets only
  `testpaths`. Registering the marker and excluding it from CI is a small M17
  task, not a given. Fixtures live in `apps/worker/tests/fixtures/fdn/`.

---

## Out of scope

Explicitly not part of this module; do not implement opportunistically.

- 13F institutional holdings, short interest, congressional trading — future
  catalyst sources, same architecture, separate milestones.
- Guidance extraction from earnings releases — no endpoint exists.
- Any modelling of index-committee discretion (§"Index eligibility").
- Company news as a catalyst source — `get_latest_news` remains unwired
  (docs/02); it would also raise `concordance.py`'s statistical power (D26), but
  that is a separate procurement question.
- Securing the commercial data licence (D8, a business decision).
- Real-time data or any intraday send (D9 stands).
