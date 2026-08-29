# M20 — §5 "Where they stand": per-stock indicator standing

*Design spec. 2026-08-29. Builds on M19 (technical snapshot, D35) and M13
(attribution consumers). Reverses a stated `docs/01` non-goal — see below.*

## What this adds

§4 answers *where does each position stand in its own price structure*: moving
average distances, levels, volume multiples, the 52-week range. What it cannot
say is whether any of those numbers is **unusual for that stock**. An RSI of 71
means one thing on a utility and another on ASTS. A 3.1% ATR is compression on
one name and expansion on another. A bare indicator value is a number the reader
has to have a prior about, and across eight names nobody has eight priors.

M20 adds **§5, "Where they stand"** — per stock, a small set of indicators each
shipped as a **pair**: the value, and its percentile against that same symbol's
own trailing 252 sessions of that same indicator.

The percentile is the product. `RSI 71` is soup; `RSI 71 · 88th percentile, 1y`
is a comparison the brief has already made for you, and it is auditable against
`bars_daily` the same way M19's `tested 4×, last 06/12` is.

---

## The non-goal being reversed

`docs/01-product.md` currently lists as a non-goal:

> **No technical indicator soup.** RSI/MACD/Bollinger across eight names is noise.

That was right about bare values and is reversed here deliberately, under three
conditions that are the whole reason the reversal is defensible:

1. **No oscillator ever ships as a bare value.** Every one carries its own-history
   percentile, or renders `—`. A value with no percentile is not published.
2. **Three orthogonal measures, not a panel.** RSI, Stochastic %K, Williams %R,
   CCI and Bollinger %B all answer "where is price within its recent range" and
   their percentiles move together. Only RSI is carried. MACD histogram (trend
   acceleration) and ADX (trend strength irrespective of direction) are carried
   because they measure something RSI does not.
3. **The email section is gated and capped.** Three names maximum, and only for
   a percentile at or beyond the decile, a divergence, or a §4 breakout. The
   ungated full grid lives on the web archive, which nobody reads by accident.

*Reverses back if:* after a month of real briefs the gate fires for most names
on most sessions — that would mean the percentiles are not discriminating, which
is the same failure M19 caught when its first touch predicate scored ASTS at 61
touches in 276 sessions. The fallback is divergence-only.

---

## §5 is not §4, and the difference is the design

| | §4 `tape_quality` | §5 `standing` |
|---|---|---|
| Question | Where does this position stand in its price structure? | Is any of that unusual *for this name*? |
| Coverage | Every owned name, **untiered** (D35) | **Gated**, capped at 3 in the email; all names on the archive |
| Shape | One table, one row per name | One block per name |
| Empty state | Cannot be empty | Renders an explicit absence note |
| Untouched by this spec | — | §4's rows, columns and thresholds do not change |

Both sections stay. Folding §5's fields into §4 as more columns is what the
600px width and the "no soup" rule both forbid.

---

## What gets computed — `worker/oscillators.py`

A new module, not an extension of `technicals.py`. Three reasons:

- `technicals.py` is already 561 lines and answers a different question. The
  split is the same one that put `technicals.py` beside `compute.py` rather than
  inside it.
- The numeric contract differs (below), and mixing exact-`Fraction` code with
  quantized-`Decimal` code in one module invites using the wrong one.
- The read depth differs: `technicals.py` reads 252 bars, this needs 286.

It imports `technicals.swing_pivots` rather than reimplementing pivot detection.

### The percentile mechanic

```
pctile(x, history) = 100 * |{h in history : h < x}| / |history|
```

over the **252 prior** sessions' values of that same indicator.

Two rules, both borrowed rather than invented:

- **The measured session is never in its own baseline.** Today is ranked against
  the 252 values before it. This is the denominator discipline `technicals.py`
  already applies to `vol_vs_5d`/`vol_vs_21d` (module docstring, and
  `verify-numbers` check 6). One rule in the codebase, not two that drift.
- **A short baseline yields `None`, never a percentile.** Below 252 prior values
  the raw value still renders and the percentile renders `—`. This is the same
  refusal `high_52w` makes for a partial window: *"a field named 52-week high
  that is really a 40-week high is a lie."* A "1-year percentile" over 40
  observations is the same lie.

`BASELINE_SESSIONS = 252`, matching `technicals.LOOKBACK_SESSIONS`. Strict `<`
in the counter, so a value tied with its entire history scores 0, not 50 — ties
are common on ADX and a midpoint convention would invent movement.

### Numeric type — a deliberate departure from `technicals.py`

`technicals.py` is exact `Fraction`/`Decimal` throughout because *"nothing here
needs `sqrt`, so nothing needs float."* Oscillators need recursive EMAs, and
`Fraction` compounds the denominator by 13 on every MACD step: over a 286-session
window that is a ~1000-bit integer carrying no accuracy anyone can use.

So this module uses **`Decimal` under an explicit `localcontext(prec=28)`,
SMA-seeded, quantized to 10 decimal places at each recursion step**. Deterministic
and reproducible across runs and machines; still never `float`, which the money
rule in `CLAUDE.md` forbids and which would make the fixture snapshots unstable.

This difference is written down here so that a later reader finds a reason rather
than an inconsistency.

### The indicators

| Field | Percentile field | Definition |
|---|---|---|
| `rsi14` | `rsi14_pctile` | Wilder's RSI, 14 sessions, on `adj_c` |
| `macd_hist` | `macd_hist_pctile` | EMA12 − EMA26, less its EMA9 signal. Histogram only — the line and the signal are two more numbers saying the same thing |
| `adx14` | `adx14_pctile` | Wilder's ADX(14) from `adj_h`/`adj_l`/`adj_c` |
| `atr_pct` | `atr_pct_pctile` | `atr14 / close`. The existing `atr14` is a raw price and is not comparable between an $8 and an $800 name |
| `rel_strength` | `rel_strength_pctile` | 21-session return less the benchmark's over the same window |
| `rel_strength_benchmark` | — | Which benchmark the above was measured against (string) |
| `divergence` | — | `bullish` \| `bearish` \| `null` |

**ADX ships with no "> 25 = trending" label.** That threshold is the convention,
but the percentile is the comparison this milestone exists to make, and a
hardcoded 25 would be an unearned opinion sitting next to an earned one.

**`atr_pct` needs an ATR *series*, which M19 does not provide.** `technicals._atr`
computes today's value only, and a percentile needs 253 of them. So this module
computes the ATR series itself, under the same definition (a simple mean of the
last 14 true ranges, deliberately not Wilder's).

Two ATR implementations in one codebase is a drift risk, so it is closed by a
test rather than by a comment: **the last value of this module's ATR series must
equal `technicals._atr` on the same bars**, asserted directly. The ratio is
dimensionless, so computing it in adjusted space gives the same answer as in
price space and no rescaling is needed.

### `rel_strength` — the field the contract has been declaring since M5

`rel_strength` exists in `brief-object.schema.json` today, declared and never
populated; `docs/05` notes it as unbuilt and D35 names it as one of two clean
later additions. M20 populates it.

- Benchmark is the holding's sector `benchmark_symbol`, falling back to
  `constants.BENCHMARK_SYMBOL` (SPY) when the sector has none or its bars are
  missing.
- `rel_strength_benchmark` carries which one was used, because *"+6.1% vs XLC"*
  and *"+6.1% vs SPY"* are different claims and a reader cannot tell them apart
  from the number.
- Benchmark bars come from `bars_daily` — sector benchmarks ∪ SPY are already
  ingested (`constants.py:76`). A benchmark with an incomplete window yields
  `None` for both fields rather than a silent fallback to SPY, which would
  change the claim without saying so.

### Divergence

Bearish: over the last two pivot **highs**, price made a higher high while RSI at
that pivot was lower. Bullish is the mirror on pivot **lows**. Pivots come from
`technicals.swing_pivots` at the existing `PIVOT_K = 3` — already written, already
tested, already strict-extrema so a flat top does not emit two pivots at one price.

The two pivots must be **5 to 60 sessions apart**. Closer is noise; further is
archaeology.

The honest consequence of k=3 is that the most recent three bars can never be
pivots, so a divergence is confirmed at least three sessions after the fact. The
renderer says so (`bearish divergence, confirmed 3 sessions`) rather than
presenting it as today's news.

### Read depth

253 indicator values are needed (252 baseline + today). MACD's histogram has the
longest warmup: EMA26 seeded by SMA26 gives a first MACD line at bar 26, and its
EMA9 signal seeded by SMA9 gives a first histogram at bar 34 — 33 bars of warmup.

**`READ_DEPTH = 252 + 1 + 33 = 286` sessions.** Its own query, not a change to
`technicals._READ_BARS`.

Same adjusted-series guard as M19: any null `adj_h`/`adj_l`/`adj_c`/`adj_v` in
the window and the symbol is **skipped**, not computed from raw columns.

**This makes the documented backfill insufficient.** `docs/03-data-model.md`
currently says `backfill --days 400` after adding a holding; 400 calendar days is
≈275 sessions, short of 286. It becomes `--days 500` (≈344 sessions), and the
note is extended to cover sector benchmarks, not only holdings.

### Storage

The numeric fields land in `metrics` following M19's pattern — `metrics.value` is
`numeric` and all of them are numbers. **No migration.**

`divergence` (an enum) and `rel_strength_benchmark` (a string) are **not** stored
in `metrics`, for exactly the reason M19 kept `ma_stack` and `breakout` out of it:
coercing an enum into a `numeric` column to avoid a migration is the wrong trade.
Assembly reads them off the returned dataclass.

---

## Object shape — `schema_version` 8 → 9

New section id **`standing`**, added to the `section.id` enum. Sections are
id-keyed and the renderer fixes order, so this is purely additive.

Eleven new `row` fields, all nullable — the twelve named in the indicator
table above, less `rel_strength`, which already exists in the contract and is merely
populated for the first time. Two enums (`divergence`, and `standing` in the section id list)
follow the existing `anyOf: [{enum}, {type: null}]` convention rather than
folding `null` into the enum — the reason is already recorded on `row.tier`.

The v8 fixture is frozen alongside the existing v2/v6/v7 ones, so "old renderers
keep working" stays a test rather than a hope.

`section.note` carries the overflow line and the empty-state line; no new field.

---

## The email gate

A name qualifies when **any** of:

- any of the five tracked percentiles is `> 90` or `< 10`
- `divergence` is non-null
- §4 set `breakout` for that symbol

Comparison at the boundary is **strict** in the same direction M19 chose for
`rvol > 1.5`: exactly 90 does not qualify. One boundary convention in the brief.

**The section carries a row per owned name; the cap is expressed as a tier, not
as a deletion.** The three most stretched qualifying names get `tier: "full"`;
every other name gets `tier: "brief"`. The email renders full-tier rows only;
the archive renders all of them.

This reuses the existing `row.tier` mechanism (M5) rather than reinventing it,
and it is what lets the archive be ungated while the email is capped — without
assembly emitting two row sets, and without the renderer making the decision
itself (D16). Ordering among qualifying names is by descending `|pctile - 50|`,
ties broken by symbol so the output is deterministic; the rest follow in symbol
order.

Overflow qualifying names are still named in `section.note`, because a reader of
the *email* cannot see the brief-tier rows.

Zero qualify → the section renders an absence note, the pattern
`catalysts?.note` already establishes. Absence is information and it is cheap.

The gate lives in `assemble.py` beside `_tape_quality`, not in the renderer — the
renderer reads decisions, it does not make them (D16).

---

## Rendering

### Email

A block per qualifying name, not a table row. Each block:

- symbol, plus the divergence badge reusing `Breakout`'s badge styling
- a small table: indicator name, value, and the percentile drawn through
  **`RangeBar`**

`RangeBar` already takes a 0–1 position and already renders through Outlook's
Word engine via the `bgcolor` attribute spread. Reusing it means the percentile
bar adds no new email-client risk, which is the whole reason not to invent a
second bar primitive.

Sits immediately after §4. §4's own markup is untouched.

### Web archive

All owned names — the archive renders every row in the section, full-tier and
brief-tier alike, so the gate applies to the email only. It carries the full
value + percentile grid for all five indicators.

**The sparkline is deferred, correcting an earlier draft of this spec.** A
252-session price line with the M19 zones drawn as bands needs the price
*series*, and the BriefObject carries the levels but not the bars. The archive
page could query `bars_daily` directly — it already uses Drizzle for `briefs` —
but the README is explicit that the BriefObject is *"the single contract both
the email and the web archive render from"*, and an archive rendering from two
sources changes that contract rather than a rendering detail. It is a real
feature that needs its own decision; it is not smuggled in under M20.

---

## Docs to update

| File | Change |
|---|---|
| `docs/01-product.md` | The non-goal is **amended in place** with the three conditions above — not deleted. A reversed decision that leaves no trace is how the next reader repeats the argument. |
| `docs/03-data-model.md` | `--days 400` → `--days 500`; extend the note to sector benchmarks; add the M20 metric definitions beside M19's. |
| `docs/04-brief-object.md` | `schema_version` 9 row. |
| `docs/05-content-spec.md` | New §5; renumber rotation → 6, after hours → 7, accountability → 8. Drop the "`rel_strength` remains a declared, unpopulated field" clause from §4. |
| `docs/07-decisions.md` | **D36**, with the reverses-if clause. |
| `docs/08-milestones.md` | M20 entry with its definition of done. |

*Noted, not fixed:* the Catalysts section renders today but was never given a
number in `docs/05`. Out of scope.

---

## Validation (Definition of Done)

### Indicator correctness — known answers, no vendor

1. **RSI(14) matches a hand-computed reference.** A fixed 40-bar close series
   with the expected Wilder RSI asserted to 4dp at three separate offsets, not
   just the last bar — a seeding bug shows up early and is masked by the tail.
2. **MACD histogram matches a hand-computed reference** on the same series, again
   at three offsets.
3. **ADX(14) matches a hand-computed reference**, including one bar with an
   inside range (no directional movement either way), which is the case a naive
   +DM/−DM implementation gets wrong.
4. **Determinism.** Computing the same window twice returns byte-identical
   `Decimal` values, and computing it from a longer prefix of the same series
   returns the same values for the overlapping sessions.

### The percentile mechanic

5. **Monotone series ⇒ 100th percentile.** A strictly increasing indicator input
   puts today at 100.
6. **Today is excluded from its own baseline.** A series whose final value is an
   extreme outlier scores the same percentile whether or not that final value is
   appended to the history list — the direct test of the denominator rule.
7. **Ties use strict `<`.** A flat series scores 0, not 50.
8. **Short baseline ⇒ `None` percentile, raw value present.** 200 sessions of
   history yields `rsi14` populated and `rsi14_pctile` null. This is a *property
   of the output object*, asserted on both fields — asserting only the null would
   pass if the whole row went missing.
9. **285 bars is not enough and 286 is.** The boundary is asserted directly, so a
   later change to a warmup constant fails a test rather than silently emitting a
   percentile over 251 observations.

### Divergence

10. **A seeded higher-high / lower-RSI series fires `bearish`.**
11. **A seeded higher-high / higher-RSI series fires nothing.** The negative case
    is the one that matters; a detector that always fires passes test 10.
12. **The mirror case fires `bullish`** on lower-low / higher-RSI.
13. **Pivot spacing is enforced** at both ends: pivots 3 sessions apart and 90
    sessions apart both yield `null`.

### The gate

14. **Exactly 90 does not qualify; 90.1 does.** The strict-boundary convention,
    tested at the boundary.
15. **Four qualifying names yield three blocks and a note naming the fourth.**
16. **Zero qualifying names yield a section with an absence note and no rows** —
    not an omitted section, which the renderer would treat as "not computed".
17. **A name qualifying only via §4's `breakout`** is included even with every
    percentile mid-range, proving the three gate arms are OR'd and that §5 reads
    §4's decision rather than recomputing it.

### Data integrity

18. **A symbol with a null `adj_h` anywhere in its 286-bar window is absent from
    the result** — the M19 skip rule, re-asserted here because this module has
    its own query and could regress independently.
19. **A benchmark with an incomplete window yields null `rel_strength` and null
    `rel_strength_benchmark`**, and specifically does *not* silently fall back to
    SPY.
20. **A sector with no `benchmark_symbol` falls back to SPY and says so** in
    `rel_strength_benchmark`.

### Contract and renderers

21. **`pnpm contracts:gen` is green** and the regenerated TS types and Pydantic
    models are committed.
22. **The frozen v8 fixture still renders** through the new template, alongside
    the existing v2/v6/v7 fixtures.
23. **A frozen v9 fixture snapshot-tests the assembled object**, including one
    name with a full snapshot, one with a null percentile from short history, and
    one absent entirely.
24. **The email renders under 80KB** through `/api/render` on the real book with
    three §5 blocks present, and is checked in Gmail and Outlook — the M19 bar.
25. **§4 is byte-identical** before and after, on the same fixture. This spec
    changes §4's *documentation*, never its output.

### End to end

26. `uv run -m worker.cli brief --kind close --dry-run` on the real book produces
    a brief whose §5 blocks, percentiles and divergences are each reproducible by
    hand from `bars_daily`. The M19 precedent stands: the first plausible
    definition of a touch was wrong on real data, and only a real-book read
    caught it.

---

## Out of scope

- **The open brief.** §5 is close-only. The open brief must survive missing bars
  and gains no compute dependency (D35).
- **CN.** `worker_cn.assemble` passes no technicals while CN bar history is partly
  synthetic; it passes no oscillators either, for the same reason.
- **Watched (non-owned) names.** §4 covers owned names; §5 follows it.
- **Intraday anything.** D8/D9 stand — "current session" is today's completed EOD
  bar.
- **Stochastic, Williams %R, CCI, Bollinger %B.** Ruled out above as near-duplicates
  of RSI, not deferred.
- **Storing percentiles for historical replay.** They recompute from `bars_daily`
  on every run at no API cost, so a table would be a cache with an invalidation
  problem and no benefit.

## Open questions

1. **Is 252 the right baseline for `atr_pct`?** Volatility regimes persist longer
   than a year, so a 1-year percentile may report "expanding" through an entire
   high-vol regime. Ships at 252 for consistency with every other percentile
   here; revisit from real briefs, not in advance.
2. **Should the gate consider §5's own history** — "this is the first time in six
   months RSI cleared the 90th" — rather than only today's level? That is a
   percentile of a percentile and is deferred until the flat version has been
   read for a month.
