# CN-M4 — CN claims + accountability loop (design)

*2026-08-16. The first CN milestone past the skeleton scope D32 drew. Turns the
CN close brief into an accountable one: it commits to falsifiable calls and
grades the ones it made last session. Structural decisions land as D34 in
`session-brief/docs/07-decisions.md`.*

## Why this is the next milestone

Of the five items D32 deferred, four are blocked on something outside the repo:
CN narration and the real calendar need a CN news/events vendor (none wired —
FinancialData.net is US-listing-focused, EDGAR doesn't apply); CN flags need
fundamentals with no EDGAR equivalent; CN return attribution needs ~120
sessions of real CN bars (the live feed only just landed at CN-M3, and
`CN_BARS_LIVE` is still off) plus CN theme baskets.

CN claims is the one that needs only a design pass — and D32 named the pass it
needs: *"or CN claims land (then the claims tables need a per-market design
first)."* This is that design.

## The problem

Three pieces of the US accountability loop are hostile to a second market.

**1. Resolution is user-wide.** `resolve_due_claims(conn, user_id,
session_date)` filters on `user_id` and `outcome IS NULL` and nothing else. The
CN close fires at 15:20 CST — hours before the US close brief runs — so a CN
call to it would grade and consume the US book's due claims, and the US brief
that owed the reader those outcomes would find them already resolved. This is
the D23(3) trap, cross-market, and it is why `assemble_cn_close_and_store`
carries an explicit prohibition against calling it today.

**2. The graders hardcode the US benchmark.** `_grade_open_close` reads
`BENCHMARK_SYMBOL` (`SPY`) directly. A CN claim graded against SPY is
meaningless.

**3. Horizon ≥1 grading requires attribution CN does not have.**
`_grade_relative` grades against the sign of the realized residual from the
`attribution` table (M13/D24 — an overnight call shouldn't earn credit for
market beta). There are no CN rows in `attribution` and won't be for months.

Point 3 is the dangerous one. `_grade_relative` returns `None` when no residual
row exists, and `resolve_due_claims` reads `None` as *"no residual yet — leave
it for the next run."* So a CN claim would not fail loudly; it would sit
unresolved forever while the ledger grew, and the CN brief's "yesterday's flag,
resolved" section would stay permanently empty with nothing anywhere reporting
that the loop was dead.

## Design

**Market on the ledger, not derived.** Migration 0018 adds
`claims.market text NOT NULL DEFAULT 'US' CHECK (market IN ('US','CN'))`,
mirroring `sectors.market` from 0015. The default backfills every existing row
as US, which is what they are.

The alternative — deriving market by joining `claims` to `holdings`/`sectors` —
is wrong because *a claim outlives the position it was made about*. Sell the
name and its `holdings` row goes; the claim is still owed a grade, but the join
drops it and it never resolves. The same silent-accumulation failure as point
3, arrived at from a different direction.

The unique constraint `(user_id, symbol, claim_type, session_date)` stands
unchanged: CN symbols are exchange-suffixed (`600519.SS`) and cannot collide
with US tickers — the same reasoning `holdings UNIQUE(user_id, symbol)` rests
on in D32. The partial index moves to `(user_id, market, session_date) WHERE
outcome IS NULL`, since market is now in every resolution query's predicate.

**`market` and `benchmark` are required parameters, not defaulted.**
`resolve_due_claims(conn, user_id, session_date, *, market, benchmark)` and
`store_emitted_claims(..., *, market)`. Deliberately keyword-only and
deliberately without defaults: a default is precisely how a future call site
silently inherits `US` and eats the other book's claims. Every caller states
its market at the call site, where a reviewer can see it.

**Grading dispatches on what data the market actually has.**

| Horizon | US | CN |
|---|---|---|
| 0 | open→close vs the market's benchmark | *not emitted* (no CN pre-market feed) |
| ≥1 | sign of realized residual (`attribution`) | close→close vs the market's benchmark |

Horizon 0 keeps its existing grader, with `BENCHMARK_SYMBOL` replaced by the
passed-in benchmark. Horizon ≥1 gains a second arm: `_grade_close_to_close`,
which grades the symbol's return against the benchmark's over the window from
the emit session's close to the resolve session's close. This is the pre-M13 US
grader, brought back under an explicit name rather than left implicit — CN gets
no residual decomposition, so relative return is the honest thing to grade, and
the docstring says so rather than pretending the two markets are graded alike.

When CN attribution eventually lands, CN moves to the residual arm and this
becomes a one-line dispatch change.

**The CN close brief joins the loop.** `assemble_cn_close_and_store` gains the
three calls its docstring currently forbids, in the US order:

1. `resolve_due_claims(..., market="CN", benchmark=CN_BENCHMARK)` — **before**
   the skip gate. A quiet CN session still grades what it owes; that's the US
   precedent (`assemble.py:369`) and the reason the comment there exists.
2. `shown, _ = _tier_positions(result, tape, {})` then
   `emit_claims(shown, result.benchmark_return)` — `emit_claims` is already
   pure and benchmark-relative, so it is reused unchanged. `{}` for decomp:
   CN has no attribution.
3. `store_emitted_claims(..., market="CN")` — after `_store_brief`, so a
   skipped brief emits nothing, exactly as the US path does.

The prohibition in the docstring is lifted **only for claims**, and only
because market scoping is what makes it safe. Narration and the catalyst
readers stay prohibited — they are genuinely user-wide, single-run concerns and
this milestone does not touch them.

**No contract bump.** `relative_strength` is already in the schema's
`claim_type` enum; `claims[]` and `resolved_claims[]` are already on every
BriefObject and are simply empty on CN briefs today. `market` is a ledger
column that never enters the object — the brief already carries its market in
`kind`. `schema_version` stays at 7 and `pnpm contracts:gen` stays green
without regeneration.

**The CN close template inherits the shared block.** `emails/cn/close-brief.tsx` is a thin wrapper over the shared `CloseBrief` component, passing CN-context options (`currencySymbol: "¥"`, `benchmarkLabel: "vs CSI 300"`, `kindLabel: "CN Close"`) — but the "Yesterday's flag, resolved" block lives in the shared template, gated only on `brief.resolved_claims.length > 0`, and renders no market- or currency-specific content. It appears on CN close briefs automatically, with no CN-side rendering code.

## Out of scope

Unchanged from D32: no CN narration, flags, or catalysts. No CN open-brief
claims — there is no CN pre-market quote source, so no `premarket_gap`
analogue exists; CN's loop is close→next-close at horizon 1.

## Testing

- `resolve_due_claims` scoped to `US` leaves a due CN claim untouched, and vice
  versa — the regression that guards the whole milestone.
- A CN claim emitted on session D resolves on the next XSHG session, graded
  close-to-close vs `510300.SS`, with `correct`/`wrong` matching the sign.
- A CN claim whose direction is right on raw return resolves `correct` without
  any `attribution` row present (proving the CN arm never reaches the residual
  grader).
- US claims still grade against the residual — the existing M13 test
  ("right on raw return but wrong on residual resolves `wrong`") must stay
  green, unchanged.
- End-to-end coverage — emission, persistence, and resolution — lives in the
  DB test `test_the_cn_close_emits_claims_and_resolves_the_prior_sessions`.
  `tests/cn/fixtures/cn_close_brief.json` keeps `claims: []` and
  `resolved_claims: []`: the test that produces it calls the pure `assemble()`
  directly and never routes claims through it, same as every other fixture in
  this repo. Stored v7 bodies still validate.
- Existing US claim tests pass with no edits beyond the new required kwargs.
