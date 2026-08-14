# M15 — Open brief: overnight tape + pre-market names

*Design spec. 2026-08-13. Builds on M14 (`2026-08-13-m14-open-brief-skeleton-design.md`).*

## What M15 adds

The two sections that make the open brief worth reading *before the bell* — the
ones M14 deferred because they need feeds that don't exist and are licensing-gated:

- **§2 Overnight tape** — ES/NQ futures, 10Y, DXY, VIX, WTI, plus foreign proxies
  relevant to held sectors; one "read" paragraph.
- **§3 Your names, pre-market** — only names moving >1% pre-market or carrying news;
  pre %, gap in dollars, pre-market volume as a multiple, each with a `why` line.

Plus the pre-market column in §5, salience leadership for §1, and the **horizon-0
morning claim** that closes the accountability loop the same day.

## Build strategy: seed synthetic, swap to live (approved)

Pre-market quotes and overnight futures/macro are premium-tier, licensing-sensitive
data (docs/02: "the open brief works on prior-close data plus delayed pre-market
quotes"; D8: EOD/delayed carries lighter licensing than real-time). M15 is built
against **synthetically-seeded** overnight/pre-market data behind the
`PremarketProvider` seam — a separate protocol from `MarketDataProvider` (below);
the M7 `fundamentals`/`events` pattern — so every
section, metric, and the morning claim is fully testable now and **swaps to a live
fdnpy premium feed once licensed**. Licensing stays a business decision (D8), not a
code blocker. Nothing here needs real-time data, so D9 (no intraday alerts) is
untouched — this is still a considered 08:15 read.

## Data model — reuse `quotes`, shared

Both feeds land in the shared `quotes` table (docs/03; market-data tables
carry **no `user_id`**, keyed by symbol) — but the table did not exist before this
milestone. `docs/03-data-model.md` has sketched `quotes` since day one, but
`0003_market_data` created only `raw_payloads`/`bars_daily`; nothing implemented
it (the same shape of error M14 found with `events`). **Migration `0011_quotes`**
creates it, keyed **`(symbol, session_date)`** — not the sketch's
`(symbol, captured_at)` — so every read is "the capture for session D" and
re-seeding replaces rather than duplicates:

- **Pre-market held-name quotes** → `quotes.extended_last` / `quotes.extended_v`
  (the columns were put there for exactly this), captured pre-open.
- **Overnight macro tape** → `quotes` rows for the tape symbols (futures ES/NQ,
  10Y yield, DXY, VIX, WTI, foreign-proxy ETFs), `last` vs `prev_close` giving the
  overnight change. These symbols are shared reference series, ingested once per
  symbol (docs/02 scaling shape), not per user.

Nothing above the seam actually supplies that `prev_close` on the first run:
nothing ingests daily bars for futures, yield, or forex series, and the tape's
own prior capture is itself seeded from the same recursion — with no base, §2
would be empty on every run, forever. `constants.TAPE_SEED_LEVELS` supplies a
nominal, lowest-priority base for exactly these symbols, overwritten the moment
a real prior capture exists, and the object says so: while
`constants.PREMARKET_FEED_IS_SYNTHETIC` is True, `assemble_open` marks §2 stale
whenever it has rows, and both renderers key a "synthetic feed" header marker
off that flag (D24, final-pass review). The seed is scoped to the tape only — it
must never answer for a held name with no `bars_daily` row of its own.

The four premium methods (`get_latest_prices` / `get_futures_prices` /
`get_index_quotes` / `get_forex_quotes`) do **not** widen `MarketDataProvider`.
Protocols are structural: `TiingoProvider`, the EOD provider, structurally cannot
serve pre-market prints, so widening the one shared protocol broke
`mypy --strict` (clean at the milestone's base, 6 errors after). The four methods
live on their own **`PremarketProvider` protocol** (`worker/providers/base.py`);
`FdnProvider` is intended to satisfy both once licensed, `SyntheticPremarketProvider`
only the pre-market one, and `ingest_premarket` takes a `PremarketProvider` (D24).

## Sections

### §2 Overnight tape

Per tape symbol: overnight % change (`last` vs prior `prev_close`) and level. One
narrated "read" paragraph (D19, prose-only) tying the macro backdrop to held
sectors — the model is good at "risk-off overnight, your semis proxy is soft,"
prompted with the tape figures but emitting **no number** (digit-drop guard). Foreign
proxies are chosen from the book's sectors, so the tape is relevant, not generic.

### §3 Your names, pre-market

For each held name moving **> 1% pre-market or carrying news** (the §3 threshold):
pre % (`extended_last` vs `prev_close`), **gap in dollars** (the dollars-not-percent
rule, docs/01), and **pre-market volume as a multiple**. The "or carrying news"
clause ships **half-wired**: `clears_threshold` takes a `has_news: bool` parameter
and nothing sets it — there is no news feed (docs/02: FinancialData.net
`get_latest_news`, Premium, unwired), the same shape as `short_interest`'s
threshold-without-a-source (D18). Wiring a news source flips the clause on with no
other change. Note the deliberate
distinction from close-brief tape quality (docs/05): §3 uses a **pre-market-specific
volume multiple** (vs typical pre-market volume at the same time), **not** the 30-day
RVOL — pre-market volume is too thin for RVOL to mean anything (D3 / docs/05). Each
row gets a narrated `why` line. Names not clearing the threshold are omitted (the
suppression principle), and a quiet pre-market shows a roll-up line.

### §5 pre-market column

The sector-setup rows (M14: benchmark 5d, vs SPY 5d) gain the pre-market column
(docs/05 §5), from the same pre-market quotes.

### §1 salience

`one_thing` leads on the largest pre-market gap. Once attribution M13 lands, the
salience source upgrades to the largest overnight `|resid_z|` (attribution's
cross-sectional score), the same read-time filter M13 exposes — a clean cross-feature
join, noted as a seam here.

## Horizon-0 morning claim — closing the loop same day

The open brief emits a **directional pre-market-gap claim** (a morning call:
"NAME gaps up pre-market, expect relative strength today"), **horizon 0**, resolved
at *that same day's close*. This is **not** a free ride on the existing engine:
the contract pinned `horizon_sessions` to `"minimum": 1`, `resolve_due_claims` read
only `session_date < :session_date` (excluding same-day claims), and
`_resolve_session` offset by `horizon - 1` (i.e. `-1` at horizon 0). Horizon 0 is a
genuine change to the D17 resolution engine: resolution now admits same-session
claims and, once that session has a bar, grades horizon 0 **open→close on the
emit session itself** — not close-to-close. Close-to-close (D-1's close vs. D's
close) contains the claimed pre-market gap as a sub-interval, so a name that
gapped up pre-market and fully faded intraday would still grade "correct"; the
gap is what the claim is *about*, so it cannot also be what grades it (D24,
final-pass review). Horizon-1 claims are untouched — still graded close-to-close
and still excluded from their own session (a regression test asserts both). This
is the first time the open→close same-day accountability loop (D16b) runs live. Claim
type is a new `premarket_gap`; emission is idempotent per the existing
`UNIQUE (user_id, symbol, claim_type, session_date)` key, but the **`claims` table**
also needed its own migration — contract v4 widened the JSON Schema's claim-type
enum and dropped `horizon_sessions`' minimum, but `claims` carried its own CHECK
constraints from migration `0006` (`claim_type IN (...)`, `horizon_sessions >= 1`)
that the contract bump never touched. Migration `0012_claims_premarket_gap` widens
both CHECKs to match (D24) — a contract enum and a database CHECK are two separate
sources of truth.

## Object shape

§2/§3 section IDs already exist in the enum (M14 note) — that is all that existed:
the `row` def carried no §2 or §3 fields at all (§5's `premarket` field did exist,
null since M14). The bump to **`schema_version` 4** adds §2's `level` /
`overnight_pct` / `overnight_abs`, §3's `pre_pct` / `gap_cents` /
`premarket_vol_mult`, and the two `claim` changes (the `premarket_gap` type,
`horizon_sessions` minimum dropped to 0) — a contract change, old renderers kept.
Renderers (open template +
web archive) render §2/§3 and the new row fields; `design/design-reference.html`'s
open-email tab is the visual authority. `pnpm contracts:gen` regenerates both
bindings; CI fails if dirty.

## Validation (Definition of Done)

1. **§3 threshold** — with seeded pre-market data, a name gapping >1% appears with
   pre %, gap in dollars, and a pre-market volume multiple; a name under the
   threshold is omitted; a flat pre-market yields the roll-up line.
2. **Not RVOL** — the §3 volume multiple is the pre-market-specific measure, asserted
   distinct from the close brief's 30-day RVOL.
3. **§2 read** — the overnight tape shows per-symbol overnight change and a narrated
   read; foreign proxies are selected from the book's sectors.
4. **Morning claim** — the open brief emits a horizon-0 directional claim that the
   *same session's* close brief resolves (`correct`/`wrong`), end to end.
5. **Provider swap** — the synthetic seed and a (mock) live `FdnProvider` pre-market
   path produce the same object shape, proving the seam.
6. **Back-compat** — an M14-era open brief still renders under its old
   `schema_version`; contracts clean.

**DoD:** on seeded overnight/pre-market data the open brief reads the macro tape,
lists the names gapping pre-market with dollars and a volume multiple, leads with the
biggest gap, and emits a morning claim the same day's close resolves — all behind the
provider seam so it swaps to live data when licensing lands.

## Out of scope for M15

Real-time (not delayed) data and any intraday send (D9 stands) · securing the data
licence itself (a business decision, D8) · the attribution-residual salience upgrade
(cross-references M13 but doesn't depend on it) · options/anatomy triggers (M13+
seams).
