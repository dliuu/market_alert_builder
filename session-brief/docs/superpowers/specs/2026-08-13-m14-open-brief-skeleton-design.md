# M14 — Open brief (skeleton, prior-close-native)

*Design spec. 2026-08-13.*

## What it is

The second of the two daily emails (docs/01): **08:15 ET, forward-looking — what
changed overnight, what to be ready for. No performance, no P&L.** M14 builds the
open brief end-to-end on data the pipeline **already caches** (prior-close bars,
`events`, `flags`), so a genuinely useful "day ahead" brief ships without waiting on
pre-market/futures data licensing. The overnight tape and pre-market-names sections,
which need feeds that don't exist yet, are **M15**.

This is the walking-skeleton pattern again: get the second brief real end-to-end
first, enrich with the licensing-gated data second.

## Scope boundary

The open brief's six sections (docs/05) split cleanly along what's cached:

| # | Section | M14 (cached data) | Needs M15 feeds |
|---|---|---|---|
| 1 | The one thing | ✓ (narration) | (salience improves with §3) |
| 2 | Overnight tape | — | ✓ futures / macro |
| 3 | Your names, pre-market | — | ✓ delayed pre-market quotes |
| 4 | On the clock today | ✓ (`events`: earnings/lockup/ex-div/macro) | |
| 5 | Sector setup | ✓ (benchmark 5d, vs SPY 5d) | pre-market column → M15 |
| 6 | Exposure check | ✓ (reuse M7 `flags`) | |

M14 emits §1/§4/§5/§6 and **explicitly omits §2/§3** with a one-line note, exactly
as M5 rendered a shorter close brief on a quiet day. The open brief **always sends**
(docs/05: "nothing happened overnight" is information) — no skip gate.

## The one structural obstacle: no P&L

The current `assemble()` is P&L-centric — it requires `book.day_bps` and always
emits a `book`. The open brief has neither. Resolution (approved):

- **`book` becomes nullable** in `brief-object.schema.json`; the open brief omits it
  entirely (true to "no performance, no P&L"). This is a shape change → **bump
  `schema_version`**, keep old renderers (docs/04). *(Coordinate the number with M13,
  which also bumps; each is an independent change and landing order assigns the
  value — renderers are kept for every prior version regardless.)*
- A **separate pure assembler**, `worker/assemble_open.py`, builds the open object
  *without* going through the P&L `ComputeResult` path. Shared helpers (subject line,
  claim/flag dicts, narration wiring) are factored out rather than duplicated.
  `assemble.py`'s **numeric behaviour** is untouched — the M4 fixture and invariant-3
  machinery stay exactly as certified, and the frozen fixture proves it across the
  extraction. It does change in two deliberate ways: it stops surfacing flags (see
  below) and it passes weights rather than a `ComputeResult` to `surface_flags`.

The section-`id` enum already contains `overnight_tape`, `premarket`, `calendar`,
`sector_setup`, `exposure_check` — so **no enum change**. But `book` nullability is
not the only contract change: the `row` def carries only close-brief fields
(`close`, `day_return`, `contribution_bps`, …) and §4/§5 have nothing to write into.
Note that `row` is the one def *without* `additionalProperties: false`, so the
generated Pydantic `Row` has no `extra='forbid'` and would **silently drop** unknown
keys rather than reject them — a bug that ships quietly. The bump therefore also adds:

- **§4 (`calendar`)** — `label`, `event_type`, `occurs_at`, `tag`
  (`macro` / `holding` / `watchlist`).
- **§5 (`sector_setup`)** — `sector_id`, `name`, `benchmark_symbol`, `ret_5d`,
  `vs_spy_5d`, and `premarket` (null until M15).
- **`symbol` becomes optional** on `row`. A macro release (CPI, FOMC) has no ticker,
  and today `row.required` is `["symbol"]`.

One version bump covers all of it. `additionalProperties: false` is added to `row`
in the same pass so the next milestone's stray key fails loudly.

## Architecture

`worker/assemble_open.py` — pure over cached reads, `generated_at` injected (a frozen
fixture snapshots the whole object, the M4 discipline):

- **§4 On the clock today** — read `events` for the session's macro releases,
  earnings, lockups, ex-div; tag `macro` / `holding` / `watchlist` from
  `holdings.status` (`owned` / `watching`, already in the book since M1).
- **§5 Sector setup** — per `sectors` row: benchmark trailing-5d return and vs-SPY-5d
  from `bars_daily` (a small pure helper). The pre-market column is null until M15.
- **§6 Exposure check** — `flags.surface_flags` (position risk + the weekly-capped
  correlation flag, M7); the open brief is finally the flags' intended home
  (docs/05 §6), discharging D18's "the open brief's §6 isn't built."
- **§1 The one thing** — reuse `narrate` (D19), prompted for a forward-looking read;
  still prose-only, digit-drop guard intact. Non-fatal: a key-less run still sends.
  `narrate` is close-shaped today: `build_prompt` hardcodes "today's close brief" and
  derives its symbols from the **attribution** section, so on an open brief every
  `why` is dropped and only `one_thing` survives. That is the correct M14 outcome
  (docs/05 gives §4/§5/§6 no `why` line) but it needs a forward-looking prompt
  variant, not a bare reuse.

### Three seams that are not free

The sections read cached tables, but three of the four are not simply wired up:

- **`events` has no writer and cannot hold §4.** Nothing in the repo has ever
  written to `events` — D18 seeded it synthetically for M7's flags and no seeder
  survives in the tree. Worse, the M7 table shape rejects half of §4:
  `event_type CHECK IN ('earnings','lockup','macro')` has **no `ex_div`**, and
  `symbol` is `NOT NULL` while a macro release has no ticker. So M14 needs a
  migration (widen the check, make `symbol` nullable) **and** a seeder,
  `worker/events_seed.py`, on the `themes_seed.py` pattern.
- **`surface_flags` cannot be reused unchanged.** It takes a `ComputeResult`
  because concentration needs `position.weight` — and routing the open brief through
  `compute_and_store` to manufacture one re-introduces the exact P&L path this design
  removes, *and* raises on any missing bar, which would break "always sends." Its
  signature changes to take `name_weights` + `sector_by_symbol` directly; `assemble.py`
  passes what it already has. Pure refactor, no threshold moves.
- **§5's benchmark bars are never ingested.** `sectors.benchmark_symbol` is settable
  in the book UI, but `scheduler.book_symbols()` and `cli._resolve_symbols()` both
  union held names + `SPY` only, so `bars_daily` has no rows for SMH/XLE/etc. Without
  this, §5 renders empty on real data. Both symbol resolvers union the sector
  benchmarks.

M14 adds **no new tables** and reads `events`, `bars_daily`, `sectors`, `holdings`,
`flags`. It does need one migration — widening `events` — plus the nullable-`book`
contract bump (the brief body is jsonb, so the object shape itself needs no DDL).

### The weekly flag budget moves with §6 (resolved)

`flags.last_seen` is **one clock**, and the weekly cap (docs/05: max one email
mention per week) is enforced against it. If both briefs call `surface_flags`,
whichever fires first spends the week's budget and the other goes silent — at 08:15
vs 16:45 that means the open brief always wins and the close brief's flags become
dead code that renders nothing. A silent, timing-dependent bug.

So M14 **moves the mechanism rather than sharing it**: `assemble.py` stops surfacing
and recording flags, and the block that renders them in `close-brief.tsx` is removed.
This is what D18 always said would happen — the close brief hosted flags only
because "the open brief's §6 isn't built," and now it is. The close brief keeps
`flags[]` in its object shape (still valid, just empty), so nothing about the
contract or old renderers changes.

*Reverses if:* a flag turns out to be genuinely close-relevant (an after-hours
supply event, say), in which case the cap needs a per-kind budget rather than one
`last_seen` — a schema change to `flags`, deliberately not taken now.

### On sourcing `events` live

The event calendars M14 seeds by hand do exist as a live feed — fdnpy exposes
`get_earnings_calendar`, `get_dividends_calendar` and `get_economic_calendar`
(lockups are **not** covered; IPO+180d off `get_ipo_calendar` is a derivation, not a
feed). This retires docs/02's "earnings calendar — **TBD**, resolve at M7" as a
*source* question. It does not change M14: those endpoints are **Premium tier
($69/mo, personal use only; redistribution needs Enterprise)**, which is the same D8
licensing gate M15 defers, and wiring §4 to it would import M15's blocker into the
milestone that exists to ship without one. M14 stays synthetic — but the seeder emits
rows in the **vendor calendar shape** (`symbol`, `event_type`, `occurs_at`, `label`)
so the eventual swap is a provider call, not a reshape, and
`earnings_calendar` / `dividends_calendar` / `economic_calendar` are **declared on
`MarketDataProvider` without implementations** (D12: keep the seam stable — `base.py`
already does this for `quote` / `news`).

## Rendering — a second template

"Different jobs, different templates" (docs/01) — a new React Email template
`apps/web/emails/open-brief.tsx`, selected by `kind` in the existing
`/api/render/:brief_id` endpoint (D6; the worker still makes the one HTTP call).
Web archive renders `kind=open` at `/briefs/<date>-open` — the slug route's regex
already accepts `open`, so only the body branches. Same 600px / inline-style /
<80KB / plaintext-part discipline (docs/06); the open brief is lighter than the
close brief, so headroom is not a concern.

Nullable `book` regenerates as `book?: Book` and **breaks strict TS in the existing
renderers** — `briefs/[slug]/page.tsx` dereferences `brief.book.*` unconditionally in
eight places and `close-brief.tsx`'s `Scorecard` does the same. Both are close-only
paths where the book is always present, so each guards once at its section boundary.
This is the useful half of the bump: the compiler enumerates every place that assumed
P&L, which is exactly the audit worth doing before a second `kind` exists.

## Scheduling — a second staged fire

The open brief adds an **08:15 ET** send, staged **ingest 08:00 → assemble 08:10 →
send 08:15** (docs/02), alongside the existing close heartbeat. Implementation
generalizes D20's single self-rescheduling one-shot into a **per-kind** schedule
(open fire and close fire, each computing its own next fire off the trading
calendar), so a half-day still moves the close and the open fires on any session.
Each fire pings its own Healthchecks check. This **discharges D20's explicit
reversal note** ("the open-brief pipeline lands → add its own fire at 08:15"). The
open brief's pre-open ingest in M14 is a no-op beyond what the close already
cached; M15 gives it real pre-market work.

**Per-kind is not a second delay.** D20's `next_fire` is built entirely on
`close_or_standard(d) + delay`, and 08:15 ET is not close-anchored — it is a fixed
wall-clock time that must *not* move on a half-day (the DoD requires exactly this
asymmetry). The open fire needs its own anchor, `calendar.et_time_on(d, time)`,
DST-aware via `ZoneInfo` like the existing `close_or_standard` fallback. Two anchors,
one `next_fire(now, kind)`.

The open fire also needs the **prior** session: at 08:15 on session `D` the brief is
*for* `D` but every figure comes from `D-1`'s close. `calendar` has `next_session`
but no `previous_session` — one more small addition. Unlike the close fire, the open
fire skips non-sessions entirely (ping success, do nothing) rather than polling for
a bar that will not exist for another eight hours.

## Accountability seam

The open brief is built to *become* a claim participant, but in M14 it
**emits no claims and resolves none** (resolved). The **horizon-0 morning
directional claim** — a morning call resolved at that same day's close — is the
mechanism D16b built the engine for, but it needs a pre-market signal to be
directional, so it lands with §3 in **M15**. That leaves nothing for M14 to emit:
there is no directional call judgeable from cached setup alone.

Resolution is the part that needs an explicit decision, because calling
`resolve_due_claims` at 08:15 *would* work and that is the trap. It uses `bars_daily`
as its session clock, so pre-open on `D` it grades against `D-1` — correct, and
idempotent. But it would consume the due claims before the close brief runs, leaving
the close brief's §7 "Yesterday's flag, resolved" permanently empty. **Resolution
stays in the close brief** (D16b, unchanged). The open brief carries `claims: []`
and `resolved_claims: []`.

This is a scheduling decision, not an engine change — the emit/resolve path is
untouched and ready for M15's horizon-0 claim, which resolves at the *same* day's
close and so cannot collide with §7.

## Implementation plan

Branch off `main` (M10-merged), **not** off an attribution branch — M14 shares no
file with M11/M12/M13. Two integers are assigned by merge order, and both will
conflict if assigned early:

- **`schema_version`.** M13 bumps too; whichever lands first takes **3**.
- **The Alembic revision.** `main`'s head is `0008_deliveries`. M11's unmerged
  `0009_attribution` already declares `down_revision = "0008_deliveries"`, so an M14
  migration branched off `main` that does the same produces **two Alembic heads** and
  `alembic upgrade head` fails outright. Whichever of M11/M14 merges second must
  re-point its `down_revision` at the other's revision. Cheapest handling: write the
  M14 migration last, against whatever head exists at merge time.

Steps 1–2 are the contract and land first; 3–5 are independent of each other.

**1. Contract + codegen.** Drop `book` from `required`; add the §4/§5 row fields;
make `row.symbol` optional; add `additionalProperties: false` to `row`;
`SCHEMA_VERSION = 3` in `assemble.py`. Run `pnpm contracts:gen`, then guard
`brief.book` in `briefs/[slug]/page.tsx` and `close-brief.tsx`.
*Verify:* `pnpm contracts:gen` leaves the tree clean, `pnpm --filter web typecheck`
passes, `uv run pytest` green, and a **stored v2 close-brief body still
`model_validate`s** (the back-compat assertion, DoD 6).

**2. The `events` migration + seeder.** Widen `events.event_type` to include
`ex_div`; make `symbol` nullable. Add `worker/events_seed.py` and a
`worker.cli events-seed` subcommand on the `themes_seed.py` pattern, emitting the
vendor calendar shape. Declare the three calendar methods on `MarketDataProvider`.
*Verify:* `alembic upgrade head` then `downgrade` is clean; seeded rows read back
through a §4 query; a macro row with `symbol IS NULL` inserts.

**3. `surface_flags` signature change.** Take `name_weights` +
`sector_by_symbol` instead of `ComputeResult`; adapt the one existing caller in
`assemble.py`. Behaviour-preserving.
*Verify:* `test_flags.py` / `test_flags_db.py` pass **unmodified** — if a threshold
test needs editing, the refactor changed behaviour and is wrong.

**4. `worker/assemble_open.py`.** First extract the genuinely shared helpers
(`_subject`, `_claim_dict`, `_resolved_dict`, `_round_bps`) into
`worker/assemble_shared.py` — `assemble.py`'s output must stay byte-identical, which
the M4 frozen fixture proves for free. Then `assemble_open(...)`: pure,
`generated_at` injected, no `book` key, **no skip gate**. §4 from `events` over a
forward window tagged from `holdings.status`; §5 from a pure `trailing_return(closes, 5)`
over each `sectors.benchmark_symbol` and SPY; §6 from `surface_flags` on prior-close
weights; §2/§3 emitted as `tier: "suppressed"` with a `note`. Add
`assemble_open_and_store`, `calendar.previous_session`, and wire
`worker.cli brief --kind open`.
*Verify:* new `tests/test_assemble_open.py` with a frozen fixture
(`tests/fixtures/open_brief.json`, the M4 discipline); a quiet-day case still
produces a sendable object (DoD 2); a `None` narrator still yields a valid object
(DoD 4).

**5. Ingest sector benchmarks.** Union `sectors.benchmark_symbol` into
`scheduler.book_symbols()` and `cli._resolve_symbols()`.
*Verify:* `backfill` writes `bars_daily` rows for a sector benchmark that is in no
holding.

**6. The open template.** `apps/web/emails/open-brief.tsx` + `renderOpen` in
`render.ts`; `/api/render/[briefId]/route.ts` branches on `brief.kind`. Mirror the
preview and size-check files the close brief has. Extend the archive page to render
the open sections.
*Verify:* `pnpm --filter web typecheck`; the size check reports < 80KB.

**7. Per-kind scheduler.** Add `calendar.et_time_on`; make `next_fire(now, kind)`
return the next `(instant, kind)`; two self-rescheduling one-shots;
`run_open_session_job` (skip non-sessions, own Healthchecks URL). New config:
`OPEN_SEND_ET` (default `08:15`) and `HEALTHCHECKS_OPEN_URL`.
*Verify:* `worker.cli schedule --dry-run` prints both fires interleaved with correct
next-fire times, and a half-day moves the close fire while the open stays at 08:15
(DoD 5). The nearest real half-day for the test is **2026-11-27**, the day after
Thanksgiving.

### Preconditions in the live book

Three are data, not code, and each makes a section render as nonsense rather than
fail loudly — so check them before reading §4/§5 output as a bug:

- **`events` is empty (0 rows).** §4 renders nothing until the seeder runs. Expected;
  step 2 is what fixes it.
- **Only one of two `sectors` has a `benchmark_symbol`, and it is `RKLB`** — a held
  stock, not a sector ETF. §5's "benchmark 5d" is meaningless against it. The book
  needs real benchmarks (SMH, XLE, …) entered before §5 says anything true.
- **No holding has `status = 'watching'`**, so §4's `watchlist` tag has nothing to
  tag. Needs one watchlist name in the book to exercise the third tag.

The stored 2026-08-11 close brief is `schema_version = 2` and carries **2 flags** —
it is both the back-compat fixture for DoD 6 and the visible proof of the flag move:
re-rendering it after this milestone shows those two flags gone from the close brief.

### Knock-on doc updates

Not code, but part of "done": docs/08 M14 checkbox; docs/02's vendor table (the
earnings-calendar and news **TBD**s now have a named source and a price); D18's
"exercised through the close brief" note, which this milestone discharges; and the
stale `fdn.py` comment marking `earnings_calendar` / `dividends` "consumed in M12" —
M12 is the econometrics core, and the real consumer is this milestone's §4.

## Validation (Definition of Done)

1. **Renders** — a valid `kind=open` object at `/briefs/<date>-open`, §1/§4/§5/§6
   present, §2/§3 shown as an omitted-note, `book` absent.
2. **Always sends** — no skip gate fires for `kind=open`, including on a quiet day.
3. **Snapshot** — frozen-fixture snapshot of the open object (M4 pattern), with
   `generated_at` injected.
4. **Narration non-fatal** — revoking `ANTHROPIC_API_KEY` still yields a valid,
   sendable open brief (D19 parity).
5. **Dual schedule** — a scheduler dry-run for one session shows both the open fire
   (08:15) and the close fire (16:45) with correct next-fire times, and a half-day
   moves the close without moving the open off 08:15.
6. **Back-compat** — a pre-M14 close brief still renders under its old
   `schema_version`; `pnpm contracts:gen` is clean.
7. **The close brief loses only its flags** — its frozen M4 fixture still matches
   after the shared-helper extraction, §7 still resolves yesterday's claims, and the
   only intended difference is an empty `flags[]` and no flag block in the email.

**DoD:** you can read an always-sending, forward-looking open brief on cached data
at `/briefs/<date>-open`; the scheduler fires open and close on the same session;
the object omits `book`; old renderers still work and the close pipeline changes in
exactly one intended way — §6's flags move to their documented home.

## Out of scope for M14 (→ M15)

§2 overnight tape · §3 pre-market names · the pre-market column in §5 · horizon-0
directional morning claims · any overnight/pre-market data ingest. Each has a named
seam above.
