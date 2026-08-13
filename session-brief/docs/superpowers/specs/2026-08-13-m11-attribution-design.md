# M11 — Return Attribution (walking skeleton)

*Design spec. 2026-08-13.*

## The question it answers

Did something happen to my company, or did it just move with everything else?
Attribution decomposes each name's daily move into **market + theme + idiosyncratic**,
additively, so the idiosyncratic residual can rank salience and be graded as a
falsifiable prediction. This is the only pipeline module with an opinion.

## Scope of THIS spec

This is a **walking skeleton**, not the finished econometrics. It gets the data
pipeline end-to-end with the *known-unstable* raw two-factor model, because
getting reconciliation right matters more early than getting the econometrics
perfect (author's call). Residual instability here is **expected** and is the
motivation for M12 — it is not a bug.

The full feature is three specs:

| | Scope |
|---|---|
| **M11 (this spec)** | PIT theme tables · leave-one-out equal-weight basket builder · two-factor OLS (market + raw theme) · weekly-fit / daily-score split · PM-synthesize + AM-reconcile plumbing · provisional labeling · `model_version` · validation tests. CLI-driven, scheduler-ready. |
| **M12** | Sequential orthogonalization · robust Huber estimator · EWMA (~60d half-life) · MAD residual scaling · contaminated-day fit exclusion (earnings / ex-div / index events) · stored diagnostics (β SE, residual autocorr, condition number) · capping + liquidity screening · fractional multi-theme loadings (→ multi-factor). |
| **M13** | Consumers: salience ranking in both briefs · AM read-through grader · anatomy/filings/options-divergence triggers · scheduler wiring of the PM/weekend fires · **BriefObject `schema_version` bump**. |

**Nothing in M11 touches the BriefObject.** The object is unchanged until M13,
so the M4 fixture snapshot stays green (the M6/M7/M8 precedent: no bump when the
shape doesn't change).

## Architecture

A new pure-core-plus-DB-layer stage, structured exactly like `compute.py` /
`flags.py` / `tape.py`: pure functions are unit-testable without a network,
clock, or DB; the DB layer reads normalized `bars_daily` and writes attribution's
own tables. **Zero vendor calls at render time** — everything reads from cache.

### Modules

- **`worker/baskets.py`** — pure: point-in-time membership resolution, equal-weight
  basket return, and **analytic leave-one-out**. For an equal-weight basket,
  `r_LOO(X) = (n·r_full − r_X)/(n−1)` — O(1) per name, no O(n²) recompute. DB layer
  reads `theme_members` + `bars_daily`.
- **`worker/attribution.py`** — pure: two-factor OLS fit, per-day decomposition
  (`market_bps + theme_bps + resid_bps == total_bps`, additive), cold-start
  shrinkage-to-theme-median + provisional flag. DB layer reads bars/fits, writes
  `attribution` + `attribution_fits` + `basket_returns`.
- **`worker/reconcile.py`** — PM synthesis of today's bar from `get_latest_prices`
  minute data; AM reconciliation against the official daily bar (correct-if-delta-
  exceeds-tolerance, mark `revised`).
- **`worker/providers/fdn.py`** — `FdnProvider` implementing the existing
  `MarketDataProvider`, extended with `latest_minute()` (PM synth). `earnings_calendar`
  / `dividends` are added to the provider now but consumed in M12.

Regression is **float** (needs `sqrt` / normal equations), documented "off the
money path" exactly like `flags.mean_pairwise_corr`; outputs quantize to `Decimal`
at storage. Returns are computed from **`adj_c`**, which already removes ex-div and
split gaps — this discharges most of "remove the mechanical gap before attribution"
for free. Synthetic PM bars built from *raw* minute data are the exception and are
reconciled against the adjusted official bar in the AM run.

### Scheduler-ready seam (CLI now, interface later)

Every stage is a thin function over `(conn, trade_date, now_utc, model_version)`
with an **injected clock** — never `datetime.now()` inside — mirroring D20's
`run_session_job(now_utc=…)`. M11 wraps them in three CLI subcommands; M13 wraps
the same functions in scheduler fires with no logic change.

```
attribution refit    --date D   # weekly: fit β over trailing 120 sessions → attribution_fits
attribution score    --date D --pm    # score from synthetic (PM) bars → attribution (provisional)
attribution reconcile --date D --am   # official bar → reconcile → attribution (revised)
```

Entry points:

```python
def refit(conn, fit_date, *, now_utc, model_version) -> RefitResult: ...
def score(conn, trade_date, *, now_utc, model_version, synthetic: bool) -> ScoreResult: ...
def reconcile(conn, trade_date, *, now_utc, model_version) -> ReconcileResult: ...
```

## Data model — migration `0009_attribution`

**All attribution tables are shared (no `user_id`).** The market + theme +
idiosyncratic decomposition of a symbol is a property of the symbol and the theme
model — identical for every user who holds it — so it follows the `bars_daily` /
`fundamentals` / `events` precedent (D18), not the user-keyed `metrics` table
(D14). The brief's own idempotency key confirms it: `(ticker, trade_date,
model_version)`, no `user_id`. Salience ranking (M13) becomes a read-time filter of
shared attribution over a user's held names. See D21.

```sql
themes         (id, key UNIQUE, name, sort_order)
theme_members  (id, theme_id → themes, symbol, weight, is_primary,
                effective_from, effective_to NULL)
               -- point-in-time; history is NEVER recomputed against current membership
               -- INDEX (theme_id, symbol, effective_from)
               -- `weight` is stored now but IGNORED in M11 (equal-weight basket);
               --   it feeds M12's capping / liquidity screening.
               -- `is_primary` picks a scored name's single theme for the two-factor
               --   model when it belongs to more than one basket; exactly one primary
               --   per symbol at any effective date. Fractional multi-theme loadings
               --   (dropping `is_primary` for a loadings table) are M12's multi-factor.

basket_returns (theme_id, trade_date, model_version, ret, n_members,
                synthetic, revised)
               -- PK (theme_id, trade_date, model_version)
               -- full equal-weight basket; leave-one-out derived analytically at read

attribution_fits (symbol, model_version, fit_date, window_start, window_end,
                   beta_market, beta_theme, alpha, r2, resid_scale, n_obs,
                   cold_start, diagnostics jsonb)
               -- PK (symbol, model_version, fit_date)

attribution    (symbol, trade_date, model_version,
                market_bps, theme_bps, resid_bps, total_bps,
                beta_market, beta_theme, r2, n_obs,
                provisional, cold_start, synthetic, revised, computed_at)
               -- PK (symbol, trade_date, model_version); idempotent upsert
```

- Returns/betas/bps are numeric, off the money-cents path (like `day_return`,
  `corr_20d`). Float in compute, `Decimal` at a fixed store scale (reuse compute's
  `_STORE_SCALE` convention).
- `resid_scale` is stored now but is **plain stdev** in M11 (MAD replaces it in
  M12); there is no `resid_z` column yet — normalization is an M12 concern.
- `ATTRIBUTION_MODEL_VERSION = 1` (new constant), stamped on every row (Layer 6
  "version the model", so a spec change never rewrites history).
- Themes are **seeded reference data** (a `themes seed` CLI / fixture, the way M7
  seeded `fundamentals`/`events`), not ingested. Real curation is manual and
  versioned via `effective_from`/`effective_to`.

## Estimation (M11 specifics)

- **Window** 120 trading days · **plain OLS** · all days included. EWMA, Huber, and
  contaminated-day exclusion are M12.
- **Model** `r_X = α + β_m·r_market + β_theme·r_basket_LOO + ε`. Raw theme, **no
  orthogonalization** — market and theme are ~0.85 correlated, so β's are known to
  be unstable here. That instability motivates M12; M11 only proves the plumbing.
- **Decomposition** per name per day, additive: `market_bps = β_m·r_market`,
  `theme_bps = β_theme·r_basket_LOO`, `resid_bps = ε`, summing to `total_bps`.
- **Baskets** equal-weight · point-in-time · leave-one-out. `theme_members.weight`
  is stored but unused here (M12 capping/liquidity). Each scored name is regressed
  on its `is_primary` theme's basket; fractional multi-theme loadings → M12's
  multi-factor model.
- **Cold start** `n_obs < 40`: shrink β toward the theme-median β,
  `β = w·β_ols + (1−w)·β_median`, `w = min(1, n_obs/120)`; set `cold_start` and
  `provisional`. Below the floor we never emit a confident residual from thin data.
- **Scored universe** the union of all users' held symbols. Basket-member bars are
  ingested too (this widens the ingest set — noted for M13 scheduler sizing).

## Data plumbing — PM synthesize / AM reconcile

Real, tested logic in M11 (CLI-driven); the failure it guards against is silent
synthetic/official drift slowly corrupting every fitted β.

- **PM run (18:30 ET).** Synthesize today's bar per symbol from `get_latest_prices`
  minute data (last minute's close ≈ session close) → basket returns → score with
  the current fit → write `attribution` with `synthetic = provisional = true`.
- **AM run (next day).** Official daily bar arrives (existing Tiingo ingest or
  `FdnProvider.daily_bars`) → reconcile synthetic vs official return; if
  `|Δ| > RECONCILE_TOL`, correct the stored return, set `revised = true`, re-score,
  clear `provisional`. Recompute anything downstream (basket returns for the day).
- **Idempotent** on the PK `(symbol, trade_date, model_version)`; a failed partial
  run is safely re-runnable.
- Corporate actions: official bars use `adj_c` (gaps already removed). Synthetic PM
  bars from raw minute data carry the mechanical ex-div gap; the AM reconcile
  against the adjusted official bar is what removes it in M11. (Explicit ex-div
  suppression on the synthetic bar is an M12 refinement once the dividends feed is
  wired.)

## Validation (Definition of Done)

Baked into the test suite from day one — the module earns trust by being checkable:

1. **Additivity (property test)** — `market_bps + theme_bps + resid_bps == total_bps`
   exactly, over messy inputs (the M3-contribution analogue).
2. **Null test (property test)** — shuffle the calendar; the residual distribution
   goes flat. If it doesn't, there is look-ahead leakage.
3. **Known-answer** — a synthetic earnings-day gap produces a large residual.
4. **Reconciliation** — synthetic-then-official replay with a divergent official bar
   flips `revised`, converges to the official-only result, and is re-runnable.
5. **Leave-one-out** — `r_LOO(X)` excludes X exactly; a name is never regressed on a
   basket containing itself; point-in-time membership is honored (no current-
   membership leakage into history).
6. **Frozen-fixture snapshot** — given a seeded theme + 120 days of bars, the
   `attribution` rows snapshot stably (the M4 pattern).

**DoD:** given a seeded theme and 120 sessions of bars, `attribution refit` then
`attribution score` produces additive, snapshot-stable residuals for held names; a
synthetic PM `score --pm` followed by `reconcile --am` with a divergent official bar
flips `revised` and converges; the null and known-answer tests pass.

## Explicitly out of scope for M11

Orthogonalization · Huber · EWMA · MAD scaling / `resid_z` · contaminated-day
exclusion · capping / liquidity screening · fractional multi-theme loadings ·
diagnostics beyond R² · β drift / theme dispersion / rolling α / residual momentum ·
all Layer-5 consumers · scheduler wiring · any BriefObject change. Each has a named
seam above.
