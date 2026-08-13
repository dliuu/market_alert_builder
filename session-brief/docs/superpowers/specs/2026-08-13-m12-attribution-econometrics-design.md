# M12 — Return Attribution: the econometric core

*Design spec. 2026-08-13. Builds on M11 (`2026-08-13-m11-attribution-design.md`).*

## What M12 fixes

M11 shipped a *deliberately unstable* raw two-factor model to prove the pipeline.
M12 replaces the estimator with the four choices the brief says carry most of the
weight, plus the diagnostics and derived signals that make the residual series
trustworthy and interpretable. Same shared tables, same CLI stages, same
scheduler-ready seam — only the math inside the pure core changes, plus a small
migration for the new stored fields.

**The residual series must visibly stabilize across this milestone.** That is the
observable success signal: betas that jittered week-to-week in M11 stop fighting
each other once the theme is orthogonalized against the market.

## Dependency added

`numpy` (the worker's first numerical dependency — the pipeline is otherwise pure
`Decimal`/`Fraction`). Regression, IRLS, and the diagnostics are linear algebra;
numpy keeps them correct and testable. We **hand-roll the robust IRLS** rather than
pull `statsmodels`→`scipy` — it's a dozen lines, fully unit-testable, and avoids a
heavy transitive chain. Regression stays "off the money path" (float), quantized to
`Decimal` at storage, exactly as M11 established. (M11's plain OLS can adopt numpy
too; nothing else in the pipeline touches it.)

## The four load-bearing changes

### 1. Sequential orthogonalization (the one that matters most)

Two-stage, replacing M11's single joint regression:

```
stage A:  r_basket_LOO = a + b·r_market + ρ          # residualize the basket on the market
stage B:  r_X          = α + β_m·r_market + β_θ·ρ + ε  # regress the stock on market + ρ
```

`ρ` is theme movement *beyond* the market, so `β_θ` means "sensitivity to
theme-specific movement" and the market/theme betas stop splitting shared variance
arbitrarily. The decomposition stays additive:
`market_bps = β_m·r_market`, `theme_bps = β_θ·ρ`, `resid_bps = ε`, summing to
`total_bps`. The stage-A coefficients `(a, b)` are stored per fit
(`attribution_fits.diagnostics`) so scoring can reconstruct `ρ` for any day.

### 2. Robust estimator — Huber via IRLS

Both stages fit with Huber weights (tuning constant `k ≈ 1.345·σ`, the standard),
solved by iteratively reweighted least squares. One earnings gap in the 120-day
window no longer dominates the fit. Convergence is capped at a small iteration
count with a tolerance; non-convergence falls back to the last iterate and is noted
in diagnostics.

### 3. EWMA weighting (~60d half-life)

Observations are weighted `w_t = λ^age`, `λ = 0.5^(1/60)`, combined multiplicatively
with the Huber weights inside IRLS. Recent regime dominates, and an old outlier
ages out smoothly instead of dropping off a cliff when it leaves the window.

### 4. MAD residual scaling → the salience score

`resid_scale` (stored since M11 as plain stdev) becomes
`1.4826 · MAD(residuals)` over a trailing per-name window, updated weekly with the
fit. A new `resid_z = resid_bps / resid_scale` column is the normalized,
cross-sectionally comparable salience score M13 ranks on. Stdev would be inflated by
exactly the fat-tailed event days we're detecting — a 3σ move scored as 1.4σ — so
MAD is not optional.

## Contaminated-day fit exclusion

Earnings days, ex-div days, and index-event days are **excluded from the fit sample
but still scored** — they corrupt β while being precisely the days worth measuring.
Sources (fdnpy, added to `FdnProvider`): `get_earnings_calendar` → `events`;
`get_dividends` / `get_dividends_calendar` → ex-div dates. Index-reconstitution days
are coarser; M12 uses a small seeded list and leaves a seam for a real feed. A
per-symbol per-date exclusion mask is computed at fit time (not stored as a table —
it's derived from `events` + dividends), so the fit reads bars minus contaminated
days while `score` still emits a residual for them.

## Baskets: capping + liquidity screening

Equal-weight is replaced by **equal-weight, capped, liquidity-screened** (docs
Layer 1): a liquidity screen drops thin names from a basket, and a cap stops one
mega-cap from becoming the basket. This **breaks M11's analytic leave-one-out**
(`(n·r_full − r_X)/(n−1)` only holds for pure equal weight), so LOO switches to an
explicit per-name basket recompute; `basket_returns` gains the full weighted series
and the DB layer materializes per-`(theme, excluded_symbol, date)` returns for the
scored names. This is the reversal D21 anticipated.

## Fractional multi-theme loadings → multi-factor (M12b)

The largest structural change; sequenced last within M12 and independently
shippable. `is_primary` on `theme_members` is replaced by a versioned
`theme_loadings (symbol, theme_id, loading, version)` where a symbol's loadings sum
to 1 (SNDK 0.7 semis / 0.3 memory), fixed by judgment. Stage B generalizes to
`r_X = α + β_m·r_market + Σ_θ loading_θ·β_θ·ρ_θ + ε`, with each theme basket
orthogonalized against the market independently. `theme_bps` becomes a sum over the
name's themes (optionally broken out per theme in `attribution` via a small jsonb
column). If this proves too large, it splits cleanly into its own milestone behind
the same seam.

## Diagnostics stored every refit

Into `attribution_fits.diagnostics` (jsonb) and promoted to typed columns where
queried often: R² (already stored), **β standard errors**, **residual
autocorrelation** (lag-1 / Durbin–Watson), and **condition number** of the stage-B
design matrix (the direct collinearity read — high here is the quantitative version
of "market and theme are fighting"). A name whose R² **collapses** is telling you
the theme assignment is wrong; M12 stores the collapse as a diagnostic and flags it
for the maintenance surface M13 renders.

## Derived signals (Layer 3), free from the same machinery

Computed weekly from stored fits/residuals and written to shared tables (a
`attribution_signals` table keyed `(symbol, trade_date, model_version)` or extra
`attribution` columns):

- **β drift** — 20-session change in `β_θ`; flags decoupling.
- **theme dispersion** — cross-sectional MAD of residuals within each basket;
  widening = the market stopped trading the theme as a block (per-theme, keyed on
  `theme_id`).
- **rolling α** — sustained residual drift; the closest thing to a re-rating signal.
- **residual momentum / reversal** — do a name's residuals persist or mean-revert.

These are cheap over data already stored; they feed M13 consumers but are computed
here as part of the scoring machinery.

## Data model — migration `0010_attribution_econometrics`

```sql
ALTER TABLE attribution ADD COLUMN resid_z            numeric;   -- MAD-scaled salience
ALTER TABLE attribution ADD COLUMN theme_bps_by_theme jsonb;     -- M12b multi-factor breakout
-- attribution_fits.diagnostics (jsonb) gains: beta_se, resid_autocorr, cond_number,
--   orthogonalization {a, b}, huber_converged, r2_collapsed.
CREATE TABLE theme_loadings   (symbol, theme_id → themes, loading, version,
                               PRIMARY KEY (symbol, theme_id, version));   -- M12b
CREATE TABLE attribution_signals (symbol, trade_date, model_version,
                                  beta_drift_20d, resid_momentum, rolling_alpha,
                                  PRIMARY KEY (symbol, trade_date, model_version));
CREATE TABLE theme_dispersion (theme_id, trade_date, model_version, dispersion_mad,
                               PRIMARY KEY (theme_id, trade_date, model_version));
```

`theme_members.is_primary` is dropped when `theme_loadings` lands (M12b). Bump
`ATTRIBUTION_MODEL_VERSION` to **2** — the residuals now mean something different, and
Layer-6 versioning requires that a spec change never silently rewrites history
(M11's residuals stay readable under version 1).

## Validation (Definition of Done)

Extends M11's suite:

1. **Orthogonality** — the residualized basket `ρ` is (numerically) uncorrelated
   with `r_market`; the stage-B condition number drops materially versus the M11
   joint fit on the same fixture.
2. **Stability** — on a fixture with a stable underlying relationship, `β_θ` no
   longer swings >0.3 across 20 sessions the way the M11 model did. (Genuine
   `β` changes >0.3 without a business reason become a stored instability
   diagnostic, per Layer 6.)
3. **MAD vs stdev** — on a fat-tailed residual fixture, `resid_z` scores a planted
   event materially higher than a stdev scaling would (the "3σ not 1.4σ" test).
4. **Robustness** — a single planted earnings gap moves the Huber β far less than it
   moves an OLS β on the same window; the gap day is excluded from the fit but still
   receives a (large) scored residual.
5. **Additivity preserved** — `market + theme + resid == total` still holds exactly
   after orthogonalization and (M12b) across the multi-theme sum.
6. **Leave-one-out under capping** — the explicit LOO recompute still excludes the
   name exactly; parity test against the analytic form on an uncapped fixture.
7. **Versioning** — version-1 (M11) rows are untouched; version-2 rows coexist.

**DoD:** on the M11 fixture, orthogonalization lowers the condition number and
stabilizes `β_θ`; Huber+EWMA blunts a planted earnings gap; `resid_z` (MAD) scores
fat-tailed events correctly; contaminated days are fit-excluded but scored;
diagnostics and derived signals persist every refit; additivity and versioning hold.

## Out of scope for M12 (→ M13)

Any BriefObject change · salience ranking in the briefs · the read-through grader ·
anatomy/filings/options-divergence triggers · scheduler wiring of the PM/weekend
fires · rendering the maintenance surface. M12 computes and stores; M13 consumes.
