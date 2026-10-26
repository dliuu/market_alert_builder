# Premium §6, every held name with the calls priced in dollars (M26)

**Intent.** The close brief's §6 "Premium" was built (M24) as a gated section: three names at most, only when their option premium read rich or cheap, with IV30 hidden in the email until a year of history existed. In practice, four sessions into live Cboe data, the section prints "Premium is fair across the book" or a row whose IV30 is a dash. The reader wanted something else: for every name they hold, tonight's implied vol, how it moved since yesterday, how it sits against the market, and what the calls a covered-call writer would look at actually cost in dollars, with yesterday's price beside today's. This milestone turns §6 from a gate into a ledger. It still measures and discloses; it still never says buy, sell, write or hold (D37).

The M24 spec (`2026-10-20-m21-m25-premium-design.md`) and the long-form plan doc ("IV Covered-Call Dashboard Plan", https://claude.ai/artifact/NdDi7eWfHQkRMinn54vNNq) stand for everything not amended here: the Cboe feed, the stored tables, IV30, the strip, the regime function, the guards.

## What is wrong today

Read from `main` at 39d5d2b on 2026-10-25. The production brief itself was not inspected; the diagnosis is from the code paths.

1. **IV30 is a dash in the email.** `Premium()` in `apps/web/emails/close-brief.tsx` renders IV30 only when `iv30_pctile` is non-null (the M20 rule, "never bare in email"). The percentile needs 252 prior sessions of stored `iv30`; live pulls began 2026-10-21. Every name prints "IV30 — · —" until late 2027. The archive shows the value with a dagger; the email does not.
2. **Almost no name qualifies.** `_qualifies_premium` admits only `rich` or `cheap`. With a null percentile, `rich` needs `iv_vix_ratio` at or above the book's median that session and `cheap` needs `iv_hv_spread < 0`; most names land `fair` and the section collapses to its empty note.
3. **No IV change anywhere on the row.** The prior session's `iv30` is already read into `iv30_hist` by `compute_and_store_volatility` and each strip cell carries `iv_chg`, but neither reaches `_premium_row`.
4. **No contract identity, no dollars.** The strip line says "30d ATM pays 3.2% (+2.0%)". The strike, expiry, mid and per-contract cost are in `option_strip` and `StripRow` and are dropped at the row builder.
5. **The one change shown is the wrong one for a writer.** `call_atm30_chg` is `chg_tenor_pct`: the relative change of premium-as-a-fraction-of-spot at the same tenor-and-rule cell, which may be a different contract from yesterday's. The same-contract change (`chg_contract_pct`) is computed and stored but never surfaced. Both are null on the first live session after a gap.

## Goals

- Every held name with a reading appears in the email, every session, with: IV30 and its day-over-day change; the own-year percentile when it exists and a visible "pending" otherwise; HV20 and IV − HV; the VIX close and its percentile, IV/VIX and the book median ratio; the term slope; the regime word; the earnings and thin guards; contracts writable.
- For each selected call (30-day ATM, 30-day 10% OTM, 7-day ATM): the contract (strike, expiry, days to expiry), the mid in dollars, the per-contract cost (mid × 100), the dollar change in that same contract's mid since the prior session, the premium as a percent of spot with its constant-tenor change, the contract's IV with its change, delta, open interest, and the target-miss and tenor-collapsed marks.
- The dollar total across the position's writable contracts, per selected call. Prices (`mid`, `mid_chg`) are floats like `close`; sums (`contract_cents`, `writable_cents`) are integer cents, the `docs/04` money rule.
- The archive shows the same, in a table.
- A stored v10 body renders unchanged in both renderers.

## Non-goals and anti-goals

- No action verbs, anywhere: templates, prompt, notes, column headers, test fixtures. "Writable" and "across writable contracts" are counts of what the position holds, not instructions; "if you wrote…" and "income" are out. The D37 reversal clause stands: an action verb appearing anywhere removes the section until fixed.
- No new ingest, no new vendor fields, no sector or peer IV. "The market" is the VIX close, its percentile, and the book's median IV/VIX ratio. The owner chose VIX-level only.
- No change to the `/volatility` dashboard, the open brief strip (M25), the regime function, the guards, the strip selection rules, or the stored tables. No Alembic migration.
- No percentile below a full baseline. The M20 rule on the *comparison* stands; what changes is that the *level* may be shown without it (D39).
- No Cboe `iv30_change` on the row. Our own IV30 is the number the brief shows, so our own prior IV30 is its base; mixing vendors would make the change disagree with the level.
- No `why` per row, no claims.

## Constraints

- D16: renderers show, never decide. Every guard, change and dollar figure is computed in the worker and carried on the row.
- D37's four conditions still hold: mechanical thresholds; every number carries its comparison base or is explicitly marked as lacking one; the earnings guard is always visible; no action verbs.
- `packages/contracts/brief-object.schema.json` is the source of truth; `pnpm contracts:gen` regenerates the TypeScript and Pydantic models, which are never hand-edited.
- Gmail clips at ~102KB; the close email stays under 80KB (`docs/06`, `apps/web/emails/check-size.ts`), now with a book of twenty names in the section.
- D33: never upgrade the shared Supabase database from a feature branch. No migration here, so the risk is nil, but the dry run still goes against a local Postgres with the `auth.uid()` shim.
- Decimal arithmetic in the worker under the existing 28-digit context; floats only at the row boundary via `_ratio`.
- Stale readings (a chain carried from the prior session) carry no changes: every `*_chg` is null, as today.

## Decisions

### Worker: what the reading gains

`VolReading` (`apps/worker/worker/volatility.py`) gains:

| Field | Definition |
|---|---|
| `iv30_chg` | `iv30 − iv30_prior`, vol points, where `iv30_prior` is the `iv30` metric stored for exactly `previous_session(session_date)`, the same base the strip changes use. Null when that session has no stored `iv30` (a gap of any length), and null when today's reading is stale. `_READ_HISTORY` returns values without dates, so `iv30_hist[-1]` is the *last stored* value, not the prior session's; the builder either carries `session_date` through that query or reads the prior session's metric with its own query. A stale reading stores its carried `iv30` under the stale date, so the next live session's change is measured against the carried number; that is the prior brief's number, which is the base the reader saw, and it is accepted. |
| `book_median_ratio` | the median IV/VIX ratio across the book's readings that session, the same value `regime` already receives; set in the same `replace` step. Null when no reading has a ratio. |

`StripRow` gains `mid_chg`: this contract's `mid` less its mid in the prior session's snapshot (`prior_mids[contract]`), in dollars; null when there was no prior quote. `strip_rows` already has `prior_mids` in hand; `chg_contract_pct` keeps its definition. Not persisted to `option_strip` (no migration); it is a function of two stored mids and a builder may re-derive it in `rebuild`-style code if ever needed.

Nothing new is written to `metrics`. `_store` is unchanged.

### Contract: schema 11

New top-level `$def` `call_quote` and a nullable `calls` array on the row. Row fields:

| Field | Type | Meaning |
|---|---|---|
| `iv30_chg` | number, null | day-over-day change in `iv30`, vol points |
| `vix_close` | number, null | the VIX close the ratio and percentile are measured against |
| `iv_vix_book_median` | number, null | the book's median IV/VIX ratio that session; the regime's fallback base |
| `calls` | `call_quote[]`, null | the selected calls, in fixed order: 30-day ATM, 30-day 10% OTM, 7-day ATM. An entry is present even when the cell has no contract, with its quote fields null, so the renderer can say "no two-sided 7-day quote" instead of silently dropping a line. |

`call_quote`:

| Field | Type | Meaning |
|---|---|---|
| `tenor` | 7, 14, 21 or 30 | the tenor the cell was asked for |
| `rule` | `itm10`, `atm`, `otm10` | the strike rule |
| `contract` | string, null | OCC symbol |
| `strike` | number, null | |
| `expiry` | date string, null | `YYYY-MM-DD` |
| `dte` | integer, null | calendar days to expiry from the session date |
| `moneyness` | number, null | `strike / close − 1` |
| `mid` | number, null | `(bid + ask) / 2`, dollars per share. A price, like `close`, so a float |
| `mid_chg` | number, null | same contract's mid, today less prior session, dollars per share |
| `contract_cents` | integer, null | `round(mid × 100 × 100)`: the cost of one contract, in cents (`docs/04`: money is integer cents) |
| `writable_cents` | integer, null | `contract_cents × contracts_writable`; null when `contracts_writable` is 0 (a zero would read as a price) |
| `premium_pct` | number, null | `mid / close` |
| `premium_pct_chg` | number, null | constant-tenor relative change, the existing `chg_tenor_pct` semantics |
| `time_value_pct` | number, null | |
| `iv` | number, null | vol points |
| `iv_chg` | number, null | vol points, same cell, prior session |
| `delta` | number, null | |
| `open_interest` | integer, null | |
| `target_miss` | boolean | strike landed more than 2.5% from the rule's target |
| `tenor_collapsed` | boolean | this expiry was already used by a shorter tenor |

The flat `call_atm30_pct`, `call_atm30_chg`, `call_otm30_pct`, `call_otm30_chg`, `call_atm7_pct` stay in the schema as optional so stored v10 bodies validate, and are **no longer emitted** on v11 rows. Both renderers fall back to them when `calls` is absent. The 7-day cell is now always included rather than only on a positive term slope; the slope sits beside it, so the reader has the story the M24 rule was protecting.

`SCHEMA_VERSION` becomes 11; the `docs/04` version table gains the row.

### Assembly: every name, ordered, uncapped by regime

`_premium` in `apps/worker/worker/assemble.py`:

- `present` is every held position with a reading, as today. Every present name gets `tier: "full"`.
- Order: names whose regime is `rich` or `cheap` first, by the existing `_premium_rank`; then `fair` names by the same rank; ties by symbol. The regime sorts; the guards do not. A rich name inside its earnings window sorts with the other rich names and wears its badge; a name with 0 writable contracts likewise. `_qualifies_premium` is deleted; nothing hides a present name.
- `PREMIUM_CAP` is raised from 3 to 20 in `constants.py`. Overflow past 20 keeps the existing `brief` tier and "also … — see the archive" note. A book larger than that is not expected; the cap exists for the email budget, not for emphasis.
- Note text: when some held names have no reading (no chain on either session), the note names them: "No option data for X, Y". When nothing is present: "No option data for the held names". "Premium is fair across the book" is dropped: the rows say it.
- `_premium_row` emits the new fields and the `calls` array from `r.strip`, keyed `(30, "atm")`, `(30, "otm10")`, `(7, "atm")`, in that order. `writable_cents` is null when `contracts_writable == 0`.

### Email

`Premium()` in `apps/web/emails/close-brief.tsx`, one block per name, 600px table layout as today, four kinds of line:

1. **Head.** Symbol; regime badge (muted, as today); `earnings 28 Oct` badge when the guard holds; `thin` badge when `iv30_thin`; `N contracts writable` (0 included).
2. **Own vol.** `IV30 42.1 (+3.2)` then `· 61st` when the percentile exists or `· pctile pending` when it does not (D39) `· HV20 35.0 · IV − HV +7.1 · term +2.3`. A null `iv30` is a dash with no change.
3. **Market.** `IV/VIX 2.29×`, appended to the own-vol line. The VIX close, its percentile and the book median ratio are identical on every row, so they print once in the section head note: `against each name's own year and the tape · VIX 18.4 (41st) · book median IV/VIX 1.90×`, before the synthetic and stale suffixes. A null VIX percentile prints `pctile pending`. `vix_close` and `iv_vix_book_median` stay on every row (D16: the renderer reads them from the first row that carries them; the body is self-describing).
4. **Calls.** One line per `calls` entry, in order:
   `30d ATM · 150C 21 Nov (27d) · $4.80 = $480/contract · $1,920 across 4 · 3.2% of spot (+10.3%) · $ +0.45 · IV 41.0 (+2.1) · Δ 0.52 · OI 1,204`
   with `· strike 3.1% off target` when `target_miss` and `· same expiry as 7d` when `tenor_collapsed`. An entry with no contract prints `30d ATM · no two-sided quote`. The "across N" fragment is omitted when `writable_cents` is null.

Changes print as signed with U+2212, as today. A null change prints nothing, not "(—)", so the first live session after a gap reads clean. Dollar figures use two decimals for mids and whole dollars with thousands separators for per-contract and across-writable sums.

The plaintext part must stay readable: keep the `<div>` per line and the spaces the M24 renderer added.

**Email budget.** If the twenty-name fixture exceeds 80KB, cut in this order and stop as soon as it fits: drop `OI` from the email calls line; drop `Δ`; drop `time value`. Do not lower `PREMIUM_CAP`, drop the 7-day line, or remove a name without the owner. The archive keeps every field regardless.

### Archive

`apps/web/app/briefs/[slug]/page.tsx`: the premium table gains `IV30 chg`, `VIX`, and `book median` columns. The `Calls` column becomes one sub-row per `calls` entry (or a nested table, builder's choice) carrying contract, dte, mid, per-contract and across-writable dollars, mid change, premium % and its change, IV and its change, delta, OI, and the two marks. All tiers shown, as today. The v10 fallback renders the old strip line.

### Narration

`premium_note` is now asked for whenever the section has rows (`_has_full_premium_rows` is true on every session with a reading). The prompt gains one sentence: every held name is listed; characterise which sit away from fair and why, in prose. The digit guard and the action-verb guard are unchanged.

### Docs

- `docs/04`: v11 row in the version table; `calls` and the three new row fields; the flat `call_*` fields marked "v10, no longer emitted".
- `docs/05`: the §6 row rewritten: every held name, ordered rich/cheap first, the four lines, the guards shown not gating, the calls priced in dollars.
- `docs/07`: **D39 — §6 is a ledger, not a gate, and IV30 may stand without its percentile in email.** Records: why the gate produced an empty section for a year (the percentile baseline); that the M20 "never bare in email" rule is amended for IV30 only, on the condition that the missing percentile is printed as "pending" beside the value and the two comparisons that exist from day one (IV − HV, IV/VIX) sit on the same line; that regime now orders instead of gating; that dollars per contract and across writable contracts are measurements of what is held, not a recommendation, and the words that keep it so.
- `docs/08`: M26 entry with the done-when below.
- `docs/01`: no change; D37's conditions are unchanged.

### Branch and verification

Work on this worktree's branch off `main` 39d5d2b. Local Postgres with the `auth.uid()` shim for the dry run; `.env` pointed at it. The shared Supabase database is not touched from the branch.

**Done when:**

- `volatility`: for one seeded name with two sessions, `iv30_chg` equals the difference of the two stored `iv30` metrics; on a session with no prior metric it is null; on a stale reading it is null; a strip cell whose contract was quoted yesterday carries `mid_chg` equal to the mid difference and a cell whose contract is new carries null.
- `assemble`: a book of one rich, one cheap and two fair names yields four `full` rows in the order rich, cheap, fair, fair (ties by symbol), and a rich name with `earnings_date` set still sorts first; the `calls` array has three entries in the fixed order; `writable_cents` is null when `contracts_writable` is 0; the `_premium_row` dict carries no flat `call_*` key (the stored body will carry them as `null`, since `to_contract_json` dumps every property, so the renderers treat a null `calls` as absent and a null flat field as absent); a name with a null `iv30_pctile` still appears with its `iv30`; the note names held names that have no reading; `schema_version` is 11.
- `pnpm contracts:gen` leaves the tree clean after the schema edit.
- The v10 fixture in `apps/worker/tests/fixtures/close_brief.json` (or a copy kept at v10) still renders in both renderers with the old strip line; a new v11 fixture with twenty names renders under 80KB in `pnpm --filter web email:check`.
- A grep of the templates, the narration prompt, the docs and the fixtures for the action-verb list in `narrate.py` finds nothing new.
- Full gate green: `pytest`, `ruff`, `mypy`, `pnpm --filter web typecheck`, `pnpm check`, `contracts:gen`, single Alembic head still `0020_options`.
- A dry run of the close assembly against the local database with a seeded chain prints the section with every held name.

## Left to the builder

- Whether `mid_chg` lives on `StripRow` from `strip_rows` or is derived in `_premium_row` from `mid` and `chg_contract_pct`. The spec names the field on the row; either mechanism satisfies the tests above.
- The exact email line wrapping at 600px when a calls line runs long; dropping `OI` from the email line (keeping it in the archive) is acceptable if the line wraps badly in Gmail.
- The archive's calls layout (sub-rows vs nested table).
- Test fixture construction for the twenty-name email size check.

## Calls made in this document (not decided by the owner)

- The 7-day ATM call is always included, not only on a positive term slope.
- Dollars are shown both per contract and across the position's writable contracts.
- `PREMIUM_CAP` goes to 20 rather than away, for the email budget.
- The flat `call_*` fields are kept in the schema for v10 bodies but no longer emitted.
- `iv30_chg` is strictly against the prior session's stored IV30, null across any gap.
- The VIX line is hoisted to the section head; only IV/VIX stays per name.
- Guards (earnings, thin, writable) never affect the order; regime alone does.
- Email overflow is cut by dropping OI, then delta, then time value from the calls line, never by dropping names or the 7-day line.
- Our own prior IV30 is the base for `iv30_chg`, not Cboe's `iv30_change`.
- Names with no chain are named in the section note rather than given an empty row.
