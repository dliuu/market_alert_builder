# M13 — Return Attribution: consumers, scheduling, and the object bump

*Design spec. 2026-08-13. Builds on M11 + M12
(`2026-08-13-m11-…`, `2026-08-13-m12-…`).*

## What M13 does

M11/M12 made attribution *infrastructure* — shared tables of additive, normalized,
versioned residuals, written by CLI stages. M13 makes it *load-bearing in the
product*: it wires the batch stages into the scheduler, carries residuals into the
BriefObject (the first `schema_version` bump of this feature), and turns the
residual into the thing that ranks and grades everything else.

The organizing idea from the brief: attribution "is the only module with an
opinion, and you can grade it." M13 is where the opinion reaches the reader and the
grade gets recorded.

## Scope split

M13 has a **groundable core** (everything the current repo can fully support) and a
set of **documented cross-module triggers** (consumers whose expensive downstream
modules — intraday anatomy, filings, options — don't exist in this repo yet).
Attribution exposes the trigger; building the triggered module is separate work.

**Core (this milestone):** scheduler wiring · BriefObject bump + salience ranking ·
AM read-through grader · maintenance surface · event-concordance validation.
**Triggers (seams only):** anatomy/filings gating · options divergence.

## 1. Scheduler wiring (no new logic)

M11's stages are already `(conn, trade_date, now_utc, model_version)` functions with
an injected clock. M13 wraps them in `worker/scheduler.py` alongside the existing
daily close heartbeat (D20):

- **Weekend refit** — Saturday fire → `attribution.refit` over every scored symbol.
- **PM score (18:30 ET)** — after the close pipeline → `attribution.score(synthetic=True)`
  from `get_latest_prices` minute data; writes provisional residuals.
- **AM reconcile (early next session)** — `attribution.reconcile` against the
  official `adj_c` bar; flips `revised`, clears `provisional`.

Each fire pings its own Healthchecks check (the D20 dead-man's-switch pattern), so a
missed refit or a stuck reconcile is visible. Fires are additive to the existing
one-shot self-rescheduling trigger; the pure fire-time math stays unit-tested
without a clock. No stage logic changes — this is purely the "eventually create an
interface to schedule this" step the M11 seam was built for.

## 2. BriefObject bump + salience ranking

**First `schema_version` bump of the feature.** The attribution rows in the close
brief's §3 gain the decomposition, and salience drives ordering and the lead.

Contract change (`packages/contracts/brief-object.schema.json`, then
`pnpm contracts:gen` regenerating both Pydantic and TS — CI fails if dirty):

- Each attribution row gains `market_bps`, `theme_bps`, `resid_bps`, `resid_z`,
  and `provisional`.
- Rows are **ranked by `|resid_z|`** (largest idiosyncratic move leads), not by day
  return — "largest |residual| is almost always the right lead."
- `one_thing` salience: assembly picks the lead name by top `|resid_z|` before
  narration, so the highlighted paragraph is about what actually happened to a
  company, not what moved with the tape.
- Suppression gains a residual-material exception: a name with `|resid_z|` over
  threshold gets **full** tier regardless of raw price move (the D16 `_tier` seam).

`assemble.py` reads the **shared** `attribution` table filtered to the user's held
names (the M11/D21 design paying off — no per-user attribution compute, just a
read-time filter). Renderers (React Email + web archive) render the new fields;
`design/design-reference.html` is the visual authority for the added columns.
`schema_version` → next; **old renderers are kept** (docs/04: year-old briefs must
still render).

Narration (D19) is unchanged in contract — the LLM still writes prose only, now
prompted with the decomposition ("moved with the theme" vs "moved on its own") but
still emitting **no number** (the digit-drop guard stands).

## 3. AM read-through grader (the falsifiable record)

The correct scoring target for an overnight/relative call is the **realized
residual**, not the raw return — "an overnight call shouldn't get credit for market
beta." M13 extends `claims.py`:

- Claim resolution grades predicted direction against the **sign of the realized
  `resid_bps`** (from shared `attribution`) instead of raw `sym_return − SPY_return`.
- This makes the M11/M12 residual the graded prediction — the module's falsifiable
  record. Stored per claim with `model_version`, so a re-spec doesn't rewrite past
  grades.
- The existing `relative_strength` claim (D17) is re-pointed at the residual;
  new residual-native claim types (residual momentum/reversal from M12's derived
  signals) drop in behind the same `claim_type` seam.

## 4. Maintenance surface

M12 stores an **R²-collapse** diagnostic — a name whose theme fit fell apart, which
means the theme assignment is likely wrong. M13 surfaces it as a maintenance flag
(a new `flags.py` `flag_type = "theme_misfit"`, user-keyed like other flags, low
severity, dashboard-only / not email-rate-limited into the reader's face). Also
surfaces M12's **β-instability** diagnostic (β changing >0.3 over 20 sessions
without a business reason) as a spec-health signal. This is the "feature, surfaced
as a maintenance flag" the brief calls for.

## 5. Cross-module triggers (seams only — downstream modules out of scope)

Attribution exposes the trigger; the expensive module it gates is separate work not
in this repo:

- **Anatomy / filings gating** — only names with a material residual (`|resid_z|`
  over threshold) warrant expensive intraday anatomy or filing pulls. M13 exposes a
  `material_residual` predicate other modules can gate on; it does **not** build the
  anatomy or filings modules.
- **Options divergence** — flat residual + steepening skew is the highest-value
  cross-module alert. Requires options data (fdnpy `get_option_chain` /
  `get_option_greeks`) and an options-ingest path that doesn't exist yet. M13
  documents the join (`|resid_z|` low **and** skew slope rising) as a future
  cross-module alert and stops there.

Building these out is a natural M14+; keeping them as seams here prevents M13 from
sprawling into modules the rest of the product hasn't grown.

## 6. Event-concordance validation (Layer 6)

The remaining Layer-6 check that needs a consumer context: high-`|resid_z|` days
should coincide with filings or news at a rate far above base — if they don't, the
model is measuring noise. Needs the company-news feed (fdnpy `get_latest_news`,
also unlocking D19's "give it the news headlines"). Implemented as a reportable
check over stored residuals × news dates, run periodically, not a per-brief gate.

## Data model

Mostly reads. Small additions:

- `claims` grading target: resolution reads `attribution.resid_bps`; if a stored
  `graded_on` marker is wanted, a nullable column on `claims` (else purely
  computed). Migration `0011_attribution_consumers` only if a column is added.
- `flags`: no schema change — `theme_misfit` is a new `flag_type` value in the
  existing table.
- **Contract**: `brief-object.schema.json` version bump (the real change here),
  regenerated bindings, updated renderers.

## Validation (Definition of Done)

1. **Salience** — on a fixture where a name has a small raw move but a large
   residual, it leads the brief and drives `one_thing`; a large raw move that is
   all market/theme does **not** lead.
2. **Grader** — a claim whose direction matches the realized residual sign resolves
   `correct`; one that was right on raw return but wrong on residual resolves
   `wrong` (proving beta doesn't earn credit).
3. **Object/renderer** — `pnpm contracts:gen` is clean in CI; the new fields render
   in email (Gmail + Outlook, <80KB) and web; an M11-era brief still renders under
   its old `schema_version`.
4. **Scheduler** — refit/PM/reconcile fires compute correct next-fire times
   (unit-tested, no clock) and each pings its Healthchecks check; a divergent
   official bar still flips `revised` end-to-end through the scheduled path.
5. **Maintenance** — a fixture with a collapsed-R² name raises a `theme_misfit`
   flag on the dashboard and nowhere noisier.
6. **Concordance** — on a fixture, high-residual days concentrate on planted
   news/filing dates well above the shuffled-baseline rate.

**DoD:** a scheduled PM→AM→weekend cycle runs attribution untouched; the close
brief leads with the top-residual name, renders the decomposition in both channels,
and its claims are graded against realized residuals; the maintenance surface shows
theme-misfit names; the object bump ships with old renderers intact and contracts
green.

## Out of scope for M13

The anatomy, filings, and options-ingest modules themselves (seams only) · an open
brief (no pre-market pipeline exists; the same salience ranking drops into it when
it lands) · any change to M11/M12 estimation. M13 consumes and schedules; it does
not re-open the econometrics.
