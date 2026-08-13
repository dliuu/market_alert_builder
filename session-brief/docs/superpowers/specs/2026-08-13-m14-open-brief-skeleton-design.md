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
  *without* going through the P&L `ComputeResult` path. The close assembler
  (`assemble.py`) is untouched, so the M4 fixture and invariant-3 machinery stay
  exactly as certified. Shared helpers (subject line, claim/flag dicts, narration
  wiring) are factored out rather than duplicated.

The section-`id` enum already contains `overnight_tape`, `premarket`, `calendar`,
`sector_setup`, `exposure_check` — so **no enum change**, only `book` nullability.

## Architecture

`worker/assemble_open.py` — pure over cached reads, `generated_at` injected (a frozen
fixture snapshots the whole object, the M4 discipline):

- **§4 On the clock today** — read `events` for the session's macro releases,
  earnings, lockups, ex-div; tag `macro` / `holding` / `watchlist` from the book.
- **§5 Sector setup** — per `sectors` row: benchmark trailing-5d return and vs-SPY-5d
  from `bars_daily` (a small pure helper). The pre-market column is null until M15.
- **§6 Exposure check** — reuse `flags.surface_flags` (position risk + the
  weekly-capped correlation flag, M7) unchanged; the open brief is finally the flags'
  intended home (docs/05 §6), discharging D18's "the open brief's §6 isn't built."
- **§1 The one thing** — reuse `narrate` (D19), prompted for a forward-looking read;
  still prose-only, digit-drop guard intact. Non-fatal: a key-less run still sends.

No new tables — M14 reads `events`, `bars_daily`, `sectors`, `flags`. The only
persisted change is the nullable-`book` contract bump (the body is jsonb; no Alembic
migration).

## Rendering — a second template

"Different jobs, different templates" (docs/01) — a new React Email template
`apps/web/emails/open-brief.tsx`, selected by `kind` in the existing
`/api/render/:brief_id` endpoint (D6; the worker still makes the one HTTP call).
Web archive renders `kind=open` at `/briefs/<date>-open`. Same 600px / inline-style /
<80KB / plaintext-part discipline (docs/06); the open brief is lighter than the
close brief, so headroom is not a concern.

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

## Accountability seam

The open brief becomes a **claim participant**: `assemble_open` wires the same
`claims` emit/resolve engine (D16b/D17). The **horizon-0 morning directional
claim** — a morning call resolved at that same day's close — is the mechanism D16b
built the engine for, but it needs a pre-market signal to be directional, so it
lands with §3 in **M15**. M14 makes the open brief exist and emit the (few)
claims judgeable from cached setup; the same-day resolution path is ready and
unchanged.

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

**DoD:** you can read an always-sending, forward-looking open brief on cached data
at `/briefs/<date>-open`; the scheduler fires open and close on the same session;
the object omits `book`; old renderers and the close pipeline are untouched.

## Out of scope for M14 (→ M15)

§2 overnight tape · §3 pre-market names · the pre-market column in §5 · horizon-0
directional morning claims · any overnight/pre-market data ingest. Each has a named
seam above.
