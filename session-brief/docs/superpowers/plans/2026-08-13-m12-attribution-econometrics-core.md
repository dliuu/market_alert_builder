# M12 Attribution Econometrics (core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace M11's deliberately-unstable raw two-factor OLS with the M12-core estimator — sequential orthogonalization, robust Huber/EWMA IRLS, MAD residual scaling, capped/liquidity-screened baskets with explicit leave-one-out, contaminated-day fit exclusion, stored diagnostics, and derived signals — behind the same CLI stages and shared tables.

**Architecture:** All numerical work stays in pure, numpy-backed functions that are unit-testable with no DB (`worker/robust.py` + pure additions to `worker/attribution.py` / `worker/baskets.py`). The DB layer (`refit` / `score`) orchestrates: it reads normalized bars, builds weighted baskets, excludes contaminated days from the fit sample, fits per name, and persists to attribution's own tables. Regression is float ("off the money path"), quantized to `Decimal` at storage exactly as M11 established.

**Tech Stack:** Python 3.12, `uv`, numpy (new), SQLAlchemy Core, Alembic, pytest, `mypy --strict`, `ruff`. Postgres 16 (Supabase).

## Global Constraints

- **numpy is the only new dependency.** Do not pull `scipy` / `statsmodels`. IRLS is hand-rolled.
- **Regression is float, off the money path.** Quantize to `Decimal` at storage via `attribution._q` (reuses `compute._STORE_SCALE`). Never float on the money-cents path.
- **Additivity is invariant.** `market_bps + theme_bps + resid_bps == total_bps` exactly, with `resid` as the closing term. This is invariant #3's attribution analogue — a change that breaks it must stop and fix it.
- **`ATTRIBUTION_MODEL_VERSION = 2`** this milestone. M11's version-1 rows are never rewritten; version-2 rows coexist under the same PKs' `model_version` axis.
- **Injected clock.** Every stage takes `now_utc`; never call `datetime.now()` inside the pure or DB functions.
- **This plan is M12-core only.** M12b (fractional multi-theme loadings, `theme_loadings`, dropping `theme_members.is_primary`, migration `0011`) is a separate future plan. Nothing here touches `is_primary`; `primary_theme_of()` keeps working unchanged.
- **M12 tunables** (module constants, documented as tunable): `HALF_LIFE = 60.0` (EWMA), `HUBER_K = 1.345`, `MAD_C = 1.4826`, `BASKET_CAP = 0.25` (max per-name basket weight), `MIN_DOLLAR_VOLUME = 1_000_000.0` (liquidity screen, `v × c`), `R2_COLLAPSE_FLOOR = 0.05`, `IRLS_MAX_ITER = 25`, `IRLS_TOL = 1e-6`, `BETA_DRIFT_FITS = 4` (~20 sessions of weekly fits).
- **DB tests** use the rolled-back `db_conn` fixture and are skipped when `DATABASE_URL` is unset. They require migration `0010` already applied to the target DB (`uv run alembic upgrade head`). Pure numerical tests need no DB and run everywhere.

---

## File Structure

- `apps/worker/pyproject.toml` — add `numpy` to `dependencies`.
- `apps/worker/worker/robust.py` — **NEW.** Pure numpy: EWMA weights, Huber weights, WLS solve, `irls_huber`, `mad_scale`, and diagnostics (`beta_standard_errors`, `durbin_watson`, `condition_number`). One responsibility: robust linear-algebra primitives.
- `apps/worker/worker/attribution.py` — **MODIFY.** Add pure `orthogonalize`, `TwoStageFit`, `fit_two_stage`, `decompose_ortho`; rewrite `refit` / `score` DB layer to use them, weighted LOO baskets, the exclusion mask, `resid_z`, and diagnostics storage. Bump nothing here (constant lives in `constants.py`).
- `apps/worker/worker/baskets.py` — **MODIFY.** Add pure `screen_and_cap`, `weighted_return`, `loo_weighted_return`; add DB `upsert_basket_loo_return`. Keep M11's `equal_weight_return` / analytic `leave_one_out` (used by the parity test).
- `apps/worker/worker/exclusions.py` — **NEW.** DB read: `contaminated_days(conn, symbols, start, end)` from `events` (earnings) + `index_events`.
- `apps/worker/worker/attribution_signals.py` — **NEW.** Derived signals (`beta_drift_20d`, `resid_momentum`, `rolling_alpha`) + `theme_dispersion`, written to their tables.
- `apps/worker/worker/constants.py` — **MODIFY.** `ATTRIBUTION_MODEL_VERSION = 2`.
- `apps/worker/worker/cli.py` — **MODIFY.** Add `attribution signals --date` subcommand.
- `apps/worker/alembic/versions/0010_attribution_econometrics.py` — **NEW** migration.
- Tests: `tests/test_robust.py` (NEW), `tests/test_baskets.py` (extend), `tests/test_attribution.py` (extend), `tests/test_exclusions.py` (NEW), `tests/test_attribution_signals.py` (NEW), `tests/test_attribution_migration.py` (extend), `tests/test_attribution_snapshot.py` (regenerate for v2), plus a new `tests/test_attribution_stability.py` (NEW, DB).

---

## Task 1: Robust IRLS core (`robust.py`)

**Files:**
- Modify: `apps/worker/pyproject.toml` (add numpy)
- Create: `apps/worker/worker/robust.py`
- Test: `apps/worker/tests/test_robust.py`

**Interfaces:**
- Produces:
  - `ewma_weights(n: int, half_life: float = 60.0) -> np.ndarray` — length-n, oldest→newest, most-recent weight 1.0.
  - `huber_weights(resid: np.ndarray, scale: float, k: float = 1.345) -> np.ndarray`
  - `wls(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray` — coefficient vector.
  - `mad_scale(resid: np.ndarray) -> float` — `1.4826 * MAD` about the median.
  - `IRLSResult(beta: np.ndarray, resid: np.ndarray, weights: np.ndarray, scale: float, converged: bool, iters: int)`
  - `irls_huber(X, y, prior_w=None, *, k=1.345, max_iter=25, tol=1e-6) -> IRLSResult`

- [ ] **Step 1: Add numpy to dependencies**

In `apps/worker/pyproject.toml`, add `"numpy>=2.0"` to the `dependencies` list (after `"exchange-calendars>=4.13.2",`).

- [ ] **Step 2: Sync the dependency**

Run: `cd apps/worker && uv sync`
Expected: resolves and installs numpy; exit 0.

- [ ] **Step 3: Write the failing tests**

Create `apps/worker/tests/test_robust.py`:

```python
from __future__ import annotations

import numpy as np
import pytest

from worker.robust import (
    ewma_weights,
    huber_weights,
    irls_huber,
    mad_scale,
    wls,
)


def test_ewma_weights_decay_to_the_oldest_and_end_at_one() -> None:
    w = ewma_weights(60, half_life=60.0)
    assert w[-1] == pytest.approx(1.0)          # most recent
    assert w[0] == pytest.approx(0.5 ** (59 / 60))  # oldest
    assert np.all(np.diff(w) > 0)               # monotone increasing toward now


def test_wls_with_unit_weights_matches_ols() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=50)
    X = np.column_stack([np.ones(50), x])
    y = 2.0 + 3.0 * x + rng.normal(scale=0.01, size=50)
    b = wls(X, y, np.ones(50))
    assert b[0] == pytest.approx(2.0, abs=0.05)
    assert b[1] == pytest.approx(3.0, abs=0.05)


def test_huber_weights_downweight_only_large_residuals() -> None:
    resid = np.array([0.0, 1.0, 10.0])
    w = huber_weights(resid, scale=1.0, k=1.345)
    assert w[0] == pytest.approx(1.0)
    assert w[1] == pytest.approx(1.0)           # within k
    assert w[2] < 0.2                           # far outlier, heavily downweighted


def test_mad_scale_is_robust_to_a_single_spike() -> None:
    clean = np.array([0.1, -0.1, 0.05, -0.05, 0.0])
    spiked = np.append(clean, 100.0)
    # MAD barely moves; stdev explodes.
    assert mad_scale(spiked) < 3 * mad_scale(clean)
    assert np.std(spiked, ddof=1) > 20 * np.std(clean, ddof=1)


def test_irls_huber_beta_resists_an_outlier_that_moves_ols() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=120)
    X = np.column_stack([np.ones(120), x])
    y = 1.0 * x + rng.normal(scale=0.01, size=120)
    y[0] += 50.0  # planted gap
    ols = wls(X, y, np.ones(120))
    rob = irls_huber(X, y)
    assert rob.converged
    assert abs(rob.beta[1] - 1.0) < abs(ols[1] - 1.0)   # Huber closer to truth
    assert abs(rob.beta[1] - 1.0) < 0.1


def test_irls_huber_reports_non_convergence_without_raising() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=30)
    X = np.column_stack([np.ones(30), x])
    y = x + rng.normal(scale=0.01, size=30)
    res = irls_huber(X, y, max_iter=1, tol=0.0)
    assert res.iters == 1
    assert res.converged is False
    assert res.beta.shape == (2,)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd apps/worker && uv run pytest tests/test_robust.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker.robust'`.

- [ ] **Step 5: Implement `robust.py`**

Create `apps/worker/worker/robust.py`:

```python
"""Robust linear-algebra primitives for attribution (M12). Pure numpy, off the
money path; callers quantize to Decimal at storage. No DB, clock, or network.

IRLS = iteratively reweighted least squares with Huber weights, hand-rolled to
avoid a scipy/statsmodels transitive chain (a dozen lines, fully unit-testable).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAD_C = 1.4826
HUBER_K = 1.345


def ewma_weights(n: int, half_life: float = 60.0) -> np.ndarray:
    """w_t = lambda^age, lambda = 0.5**(1/half_life). Oldest obs has the largest
    age; the most recent obs (index n-1) has age 0 and weight 1.0. Returned
    oldest->newest to align with a chronologically sorted series."""
    lam = 0.5 ** (1.0 / half_life)
    ages = np.arange(n - 1, -1, -1, dtype=float)
    return lam ** ages


def huber_weights(resid: np.ndarray, scale: float, k: float = HUBER_K) -> np.ndarray:
    """Huber psi weights: 1 inside k*scale, k*scale/|resid| outside. A zero scale
    (perfect fit) means no outliers to downweight -> all ones."""
    if scale == 0:
        return np.ones_like(resid)
    a = np.abs(resid) / scale
    w = np.ones_like(resid)
    mask = a > k
    w[mask] = k / a[mask]
    return w


def wls(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least squares: solve (X^T W X) b = X^T W y via the sqrt-weight
    trick and lstsq (stable for near-collinear designs)."""
    sw = np.sqrt(w)
    b, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
    return b


def mad_scale(resid: np.ndarray) -> float:
    """1.4826 * MAD about the median — a robust stdev estimate. Stdev would be
    inflated by exactly the fat-tailed event days we are detecting (spec)."""
    med = np.median(resid)
    mad = np.median(np.abs(resid - med))
    return float(MAD_C * mad)


@dataclass(frozen=True)
class IRLSResult:
    beta: np.ndarray
    resid: np.ndarray
    weights: np.ndarray
    scale: float
    converged: bool
    iters: int


def irls_huber(
    X: np.ndarray, y: np.ndarray, prior_w: np.ndarray | None = None, *,
    k: float = HUBER_K, max_iter: int = 25, tol: float = 1e-6,
) -> IRLSResult:
    """Huber IRLS. `prior_w` (e.g. EWMA) multiplies the Huber weights each
    iteration, so recency and robustness compose. Convergence is capped; on
    non-convergence we return the last iterate with converged=False (spec)."""
    n = X.shape[0]
    base = np.ones(n) if prior_w is None else np.asarray(prior_w, dtype=float)
    beta = wls(X, y, base)
    resid = y - X @ beta
    scale = mad_scale(resid)
    w = base
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        w = base if scale == 0 else base * huber_weights(resid, scale, k)
        new_beta = wls(X, y, w)
        resid = y - X @ new_beta
        scale = mad_scale(resid)
        if float(np.max(np.abs(new_beta - beta))) < tol:
            beta = new_beta
            converged = True
            break
        beta = new_beta
    return IRLSResult(beta=beta, resid=resid, weights=w, scale=scale, converged=converged, iters=it)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd apps/worker && uv run pytest tests/test_robust.py -q`
Expected: PASS (6 passed).

- [ ] **Step 7: Typecheck and lint**

Run: `cd apps/worker && uv run mypy worker/robust.py && uv run ruff check worker/robust.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add apps/worker/pyproject.toml apps/worker/uv.lock apps/worker/worker/robust.py apps/worker/tests/test_robust.py
git commit -m "feat(m12): robust IRLS core (Huber + EWMA + MAD) behind numpy"
```

---

## Task 2: Regression diagnostics (`robust.py`)

**Files:**
- Modify: `apps/worker/worker/robust.py`
- Test: `apps/worker/tests/test_robust.py`

**Interfaces:**
- Produces:
  - `beta_standard_errors(X: np.ndarray, resid: np.ndarray, w: np.ndarray) -> np.ndarray`
  - `durbin_watson(resid: np.ndarray) -> float`
  - `condition_number(X: np.ndarray) -> float`

- [ ] **Step 1: Write the failing tests**

Append to `apps/worker/tests/test_robust.py`:

```python
from worker.robust import beta_standard_errors, condition_number, durbin_watson


def test_beta_standard_errors_shrink_with_a_tighter_fit() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=100)
    X = np.column_stack([np.ones(100), x])
    w = np.ones(100)
    tight = beta_standard_errors(X, rng.normal(scale=0.001, size=100), w)
    loose = beta_standard_errors(X, rng.normal(scale=0.1, size=100), w)
    assert np.all(tight < loose)
    assert tight.shape == (2,)


def test_durbin_watson_near_two_for_white_noise() -> None:
    rng = np.random.default_rng(4)
    assert durbin_watson(rng.normal(size=500)) == pytest.approx(2.0, abs=0.2)


def test_condition_number_high_for_collinear_columns() -> None:
    rng = np.random.default_rng(5)
    m = rng.normal(size=200)
    near = m + rng.normal(scale=1e-4, size=200)  # ~market, as in the M11 joint fit
    collinear = np.column_stack([np.ones(200), m, near])
    orthogonal = np.column_stack([np.ones(200), m, rng.normal(size=200)])
    assert condition_number(collinear) > 10 * condition_number(orthogonal)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/worker && uv run pytest tests/test_robust.py -k "standard_errors or durbin or condition" -q`
Expected: FAIL with ImportError for the new names.

- [ ] **Step 3: Implement the diagnostics**

Append to `apps/worker/worker/robust.py`:

```python
def beta_standard_errors(X: np.ndarray, resid: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted OLS coefficient standard errors: sqrt(diag(sigma2 * (X^T W X)^-1)),
    sigma2 = sum(w * resid^2) / (n - p). Uses pinv for near-singular designs."""
    n, p = X.shape
    dof = max(n - p, 1)
    sigma2 = float(np.sum(w * resid**2) / dof)
    xtwx = X.T @ (w[:, None] * X)
    cov = sigma2 * np.linalg.pinv(xtwx)
    return np.sqrt(np.abs(np.diag(cov)))


def durbin_watson(resid: np.ndarray) -> float:
    """Lag-1 residual autocorrelation read: sum((e_t - e_{t-1})^2) / sum(e_t^2).
    ~2 = no autocorrelation; ->0 = positive autocorrelation (look-ahead smell)."""
    denom = float(np.sum(resid**2))
    if denom == 0:
        return 2.0
    return float(np.sum(np.diff(resid) ** 2) / denom)


def condition_number(X: np.ndarray) -> float:
    """2-norm condition number of the design matrix — the direct collinearity
    read. High = 'market and theme are fighting' (spec)."""
    return float(np.linalg.cond(X))
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/worker && uv run pytest tests/test_robust.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/robust.py && uv run ruff check worker/robust.py
git add apps/worker/worker/robust.py apps/worker/tests/test_robust.py
git commit -m "feat(m12): beta SE, Durbin-Watson, condition-number diagnostics"
```

---

## Task 3: Sequential orthogonalization + two-stage fit (`attribution.py` pure)

**Files:**
- Modify: `apps/worker/worker/attribution.py`
- Test: `apps/worker/tests/test_attribution.py`

**Interfaces:**
- Consumes: `robust.{ewma_weights, irls_huber, mad_scale, beta_standard_errors, durbin_watson, condition_number}`.
- Produces:
  - `orthogonalize(r_basket, r_market, w) -> tuple[float, float, np.ndarray]` returning `(a, b, rho)`.
  - `TwoStageFit` dataclass with: `alpha, beta_market, beta_theta, a, b, r2, resid_scale, beta_se (tuple[float,float,float]), durbin_watson, cond_number, huber_converged, r2_collapsed, n_obs, cold_start`.
  - `fit_two_stage(r_x, r_market, r_basket, *, half_life=HALF_LIFE) -> TwoStageFit`
  - `decompose_ortho(fit: TwoStageFit, r_market: float, r_basket: float, r_x: float) -> Decomposition`
  - Module constants `HALF_LIFE = 60.0`, `R2_COLLAPSE_FLOOR = 0.05`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/worker/tests/test_attribution.py`:

```python
import numpy as np

from worker.attribution import (
    HALF_LIFE,
    decompose_ortho,
    fit_two_stage,
    orthogonalize,
)


def test_orthogonalize_decorrelates_the_basket_from_the_market() -> None:
    # Stage A is robust (Huber IRLS), so rho is orthogonal to the market under the
    # WEIGHTED inner product; unweighted correlation is small-but-nonzero, not
    # machine-zero. What matters: the ~0.85 raw basket/market correlation collapses.
    rng = np.random.default_rng(10)
    r_market = rng.normal(scale=0.01, size=200)
    r_basket = 0.9 * r_market + rng.normal(scale=0.003, size=200)  # ~0.85 corr, as M11
    w = np.ones(200)
    a, b, rho = orthogonalize(r_basket, r_market, w)
    raw = abs(np.corrcoef(r_basket, r_market)[0, 1])
    resid = abs(np.corrcoef(rho, r_market)[0, 1])
    assert raw > 0.8                # raw basket tracks the market
    assert resid < 0.05             # rho is numerically decorrelated
    assert resid < raw / 10         # ...by more than an order of magnitude


def test_two_stage_additivity_holds_exactly() -> None:
    r_x, r_m, r_t = _series(160, seed=11)
    fit = fit_two_stage(r_x, r_m, r_t)
    for m, t, x in zip(r_m, r_t, r_x, strict=True):
        d = decompose_ortho(fit, m, t, x)
        assert d.market_bps + d.theme_bps + d.resid_bps == pytest.approx(d.total_bps, abs=1e-9)


def test_two_stage_lowers_condition_number_versus_joint_ols() -> None:
    # The same near-collinear market/theme that destabilizes the M11 joint fit.
    rng = np.random.default_rng(12)
    r_m = list(rng.normal(scale=0.01, size=160))
    noise_t = rng.normal(scale=0.002, size=160)
    noise_x = rng.normal(scale=0.004, size=160)
    r_t = [m + n for m, n in zip(r_m, noise_t, strict=True)]      # ~market, near-collinear
    r_x = [0.3 * m + 0.5 * t + n
           for m, t, n in zip(r_m, r_t, noise_x, strict=True)]
    joint = np.linalg.cond(np.column_stack([np.ones(160), r_m, r_t]))
    fit = fit_two_stage(r_x, r_m, r_t)
    assert fit.cond_number < joint


def test_decompose_ortho_reconstructs_rho_from_stored_ab() -> None:
    r_x, r_m, r_t = _series(160, seed=13)
    fit = fit_two_stage(r_x, r_m, r_t)
    # theme_bps must equal beta_theta * (r_basket - (a + b*r_market)) * 10_000.
    m, t, x = r_m[0], r_t[0], r_x[0]
    rho = t - (fit.a + fit.b * m)
    d = decompose_ortho(fit, m, t, x)
    assert d.theme_bps == pytest.approx(fit.beta_theta * rho * 10_000, abs=1e-9)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/worker && uv run pytest tests/test_attribution.py -k "orthogonal or two_stage or decompose_ortho" -q`
Expected: FAIL with ImportError for the new names.

- [ ] **Step 3: Implement the pure two-stage core**

In `apps/worker/worker/attribution.py`, add the numpy import and new members. After the existing `import statistics` line add:

```python
import numpy as np

from worker.robust import (
    beta_standard_errors,
    condition_number,
    durbin_watson,
    ewma_weights,
    irls_huber,
    mad_scale,
)
```

Add constants near `FIT_WINDOW`:

```python
HALF_LIFE = 60.0
R2_COLLAPSE_FLOOR = 0.05
```

Add the pure functions (place them after `decompose`, before `_solve3`):

```python
@dataclass(frozen=True)
class TwoStageFit:
    alpha: float
    beta_market: float
    beta_theta: float
    a: float                 # stage-A intercept: r_basket = a + b*r_market + rho
    b: float                 # stage-A market slope
    r2: float | None
    resid_scale: float | None  # MAD scale (1.4826 * MAD)
    beta_se: tuple[float, float, float]
    durbin_watson: float
    cond_number: float
    huber_converged: bool
    r2_collapsed: bool
    n_obs: int
    cold_start: bool


def orthogonalize(
    r_basket: np.ndarray | list[float], r_market: np.ndarray | list[float],
    w: np.ndarray,
) -> tuple[float, float, np.ndarray]:
    """Stage A: residualize the basket on the market (robust, EWMA-weighted).
    Returns (a, b, rho) where rho = r_basket - (a + b*r_market) is theme movement
    beyond the market. rho is orthogonal to r_market by construction of the WLS
    normal equations, so beta_theta stops splitting shared variance arbitrarily."""
    rb = np.asarray(r_basket, dtype=float)
    rm = np.asarray(r_market, dtype=float)
    X = np.column_stack([np.ones_like(rm), rm])
    res = irls_huber(X, rb, prior_w=w)
    a, b = float(res.beta[0]), float(res.beta[1])
    rho = rb - (a + b * rm)
    return a, b, rho


def fit_two_stage(
    r_x: list[float], r_market: list[float], r_basket: list[float], *,
    half_life: float = HALF_LIFE,
) -> TwoStageFit:
    """Two-stage sequential-orthogonalization fit with robust Huber/EWMA IRLS.
    Series are chronological (oldest first) so EWMA recency aligns. Additive
    decomposition: market = beta_m*r_market, theme = beta_theta*rho, resid = eps."""
    n = len(r_x)
    if not (n == len(r_market) == len(r_basket)):
        raise ValueError("mismatched series lengths")
    if n < 2:
        raise ValueError("need at least 2 observations to fit")

    y = np.asarray(r_x, dtype=float)
    rm = np.asarray(r_market, dtype=float)
    w = ewma_weights(n, half_life)

    a, b, rho = orthogonalize(r_basket, rm, w)
    X = np.column_stack([np.ones(n), rm, rho])
    res = irls_huber(X, y, prior_w=w)
    alpha, beta_market, beta_theta = (float(v) for v in res.beta)

    resid = res.resid
    mean_y = float(np.mean(y))
    ss_tot = float(np.sum((y - mean_y) ** 2))
    ss_res = float(np.sum(resid**2))
    r2 = None if ss_tot == 0 else 1.0 - ss_res / ss_tot
    se = beta_standard_errors(X, resid, res.weights)

    return TwoStageFit(
        alpha=alpha,
        beta_market=beta_market,
        beta_theta=beta_theta,
        a=a,
        b=b,
        r2=r2,
        resid_scale=mad_scale(resid),
        beta_se=(float(se[0]), float(se[1]), float(se[2])),
        durbin_watson=durbin_watson(resid),
        cond_number=condition_number(X),
        huber_converged=res.converged,
        r2_collapsed=(r2 is not None and r2 < R2_COLLAPSE_FLOOR),
        n_obs=n,
        cold_start=n < COLD_START_FLOOR,
    )


def decompose_ortho(
    fit: TwoStageFit, r_market: float, r_basket: float, r_x: float
) -> Decomposition:
    """Additive decomposition using stored stage-A (a, b) to reconstruct rho for
    this day. resid is the closing term so the parts sum to total exactly."""
    rho = r_basket - (fit.a + fit.b * r_market)
    total = r_x * BPS
    market = fit.beta_market * r_market * BPS
    theme = fit.beta_theta * rho * BPS
    resid = total - market - theme
    return Decomposition(market_bps=market, theme_bps=theme, resid_bps=resid, total_bps=total)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/worker && uv run pytest tests/test_attribution.py -q`
Expected: PASS (existing M11 pure tests still green; new ones pass).

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/attribution.py && uv run ruff check worker/attribution.py
git add apps/worker/worker/attribution.py apps/worker/tests/test_attribution.py
git commit -m "feat(m12): sequential orthogonalization + robust two-stage fit"
```

---

## Task 4: Capping, liquidity screen, explicit weighted LOO (`baskets.py`)

**Files:**
- Modify: `apps/worker/worker/baskets.py`
- Test: `apps/worker/tests/test_baskets.py`

**Interfaces:**
- Produces:
  - `screen_and_cap(liquidity: dict[str, float], *, min_dollar_volume: float, cap: float) -> dict[str, float]` — survivors→weights summing to 1 (empty if none survive).
  - `weighted_return(returns: dict[str, float], weights: dict[str, float]) -> BasketReturn`
  - `loo_weighted_return(returns, liquidity, *, min_dollar_volume, cap, excluded) -> BasketReturn` — re-screens/re-caps after removing `excluded`.
  - DB: `upsert_basket_loo_return(conn, theme_id, excluded_symbol, trade_date, model_version, br, *, synthetic, revised) -> None`.
  - Module constants `BASKET_CAP = 0.25`, `MIN_DOLLAR_VOLUME = 1_000_000.0`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/worker/tests/test_baskets.py`:

```python
from worker.baskets import (
    BASKET_CAP,
    MIN_DOLLAR_VOLUME,
    loo_weighted_return,
    screen_and_cap,
    weighted_return,
)


def test_screen_drops_thin_names() -> None:
    liq = {"A": 5e6, "B": 5e6, "C": 100.0}  # C below the floor
    w = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.5)
    assert set(w) == {"A", "B"}
    assert sum(w.values()) == pytest.approx(1.0)


def test_cap_stops_one_name_from_becoming_the_basket() -> None:
    liq = {"A": 1e9, "B": 5e6, "C": 5e6, "D": 5e6, "E": 5e6}
    w = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.25)
    assert w["A"] == pytest.approx(0.25)         # capped
    assert sum(w.values()) == pytest.approx(1.0)  # excess redistributed


def test_weighted_return_is_the_weighted_mean() -> None:
    br = weighted_return({"A": 0.02, "B": -0.01}, {"A": 0.75, "B": 0.25})
    assert br.ret == pytest.approx(0.75 * 0.02 + 0.25 * -0.01)
    assert br.n_members == 2


def test_loo_parity_with_analytic_on_uncapped_equal_weight() -> None:
    # No cap binds and all names pass the screen -> equal weight -> analytic LOO.
    rets = {"A": 0.02, "B": -0.01, "C": 0.05}
    liq = {"A": 5e6, "B": 5e6, "C": 5e6}
    br = loo_weighted_return(
        rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=1.0, excluded="A"
    )
    assert br.ret == pytest.approx((-0.01 + 0.05) / 2)  # mean of {B, C}
    assert "A" not in {*rets} - {"A"} or br.n_members == 2


def test_loo_reflects_recapping_after_removal() -> None:
    # Removing the capped mega-cap frees weight; survivors re-cap equally here.
    rets = {"A": 0.10, "B": 0.00, "C": 0.00, "D": 0.00, "E": 0.00}
    liq = {"A": 1e9, "B": 5e6, "C": 5e6, "D": 5e6, "E": 5e6}
    br = loo_weighted_return(
        rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.25, excluded="A"
    )
    assert br.n_members == 4
    assert br.ret == pytest.approx(0.0)  # A (the only mover) is gone
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/worker && uv run pytest tests/test_baskets.py -k "screen or cap or weighted or loo" -q`
Expected: FAIL with ImportError for the new names.

- [ ] **Step 3: Implement the pure basket functions**

Append to the pure section of `apps/worker/worker/baskets.py` (before `# --- Database layer ---`):

```python
BASKET_CAP = 0.25
MIN_DOLLAR_VOLUME = 1_000_000.0


def screen_and_cap(
    liquidity: dict[str, float], *, min_dollar_volume: float, cap: float
) -> dict[str, float]:
    """Dollar-volume-weighted, liquidity-screened, capped weights summing to 1.
    Names below the dollar-volume floor are dropped; survivors are weighted
    proportional to dollar volume; no name exceeds `cap`; excess from capped names
    redistributes to the uncapped survivors (proportional to their base weight),
    iterated to a fixed point. If the cap is infeasible for the survivor count
    (`n * cap < 1`, e.g. fewer than 4 names at a 0.25 cap), the cap cannot hold
    while summing to 1, so we fall back to equal weight — the least-concentrated
    valid distribution. Equal weighting otherwise would make the cap vestigial —
    the cap is what stops one mega-cap from becoming the basket, and what breaks the
    analytic leave-one-out."""
    survivors = sorted(s for s, dv in liquidity.items() if dv >= min_dollar_volume)
    if not survivors:
        return {}
    n = len(survivors)
    if cap < 1.0 and n * cap < 1.0:
        return {s: 1.0 / n for s in survivors}
    dv_total = sum(liquidity[s] for s in survivors)
    base = {s: liquidity[s] / dv_total for s in survivors}
    if cap >= 1.0:
        return base

    weights = dict(base)
    capped: set[str] = set()
    for _ in range(len(survivors)):
        over = [s for s in survivors if s not in capped and weights[s] > cap + 1e-12]
        if not over:
            break
        for s in over:
            weights[s] = cap
            capped.add(s)
        uncapped = [s for s in survivors if s not in capped]
        budget = 1.0 - cap * len(capped)
        base_sum = sum(base[s] for s in uncapped)
        if not uncapped or budget <= 0 or base_sum == 0:
            break
        for s in uncapped:
            weights[s] = budget * base[s] / base_sum
    return weights


def weighted_return(returns: dict[str, float], weights: dict[str, float]) -> BasketReturn:
    """Weighted mean of member returns over the intersection of `returns` and
    `weights`, renormalized so the used weights sum to 1."""
    used = {s: weights[s] for s in weights if s in returns}
    total = sum(used.values())
    if total == 0:
        raise ValueError("empty basket: no weighted members with a return")
    ret = sum(returns[s] * used[s] for s in used) / total
    return BasketReturn(ret=ret, n_members=len(used))


def loo_weighted_return(
    returns: dict[str, float], liquidity: dict[str, float], *,
    min_dollar_volume: float, cap: float, excluded: str,
) -> BasketReturn:
    """Leave-one-out basket return under capping/screening: remove `excluded`,
    then re-screen and re-cap the survivors (the freed weight redistributes and
    the cap can re-bind). This is why analytic O(1) LOO no longer holds (spec)."""
    liq = {s: dv for s, dv in liquidity.items() if s != excluded}
    weights = screen_and_cap(liq, min_dollar_volume=min_dollar_volume, cap=cap)
    return weighted_return(returns, weights)
```

- [ ] **Step 4: Implement the LOO upsert (DB layer)**

Append to the DB layer of `apps/worker/worker/baskets.py`:

```python
_UPSERT_BASKET_LOO = text("""
    INSERT INTO basket_loo_returns
        (theme_id, excluded_symbol, trade_date, model_version, ret, n_members)
    VALUES (:theme_id, :excluded_symbol, :trade_date, :model_version, :ret, :n_members)
    ON CONFLICT (theme_id, excluded_symbol, trade_date, model_version) DO UPDATE
        SET ret = EXCLUDED.ret, n_members = EXCLUDED.n_members
""")


def upsert_basket_loo_return(
    conn: Connection, theme_id: str, excluded_symbol: str, trade_date: date,
    model_version: int, br: BasketReturn,
) -> None:
    conn.execute(_UPSERT_BASKET_LOO, {
        "theme_id": theme_id, "excluded_symbol": excluded_symbol,
        "trade_date": trade_date, "model_version": model_version,
        "ret": br.ret, "n_members": br.n_members,
    })
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd apps/worker && uv run pytest tests/test_baskets.py -q`
Expected: PASS (M11 analytic tests still green; new ones pass).

- [ ] **Step 6: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/baskets.py && uv run ruff check worker/baskets.py
git add apps/worker/worker/baskets.py apps/worker/tests/test_baskets.py
git commit -m "feat(m12): capped, liquidity-screened baskets + explicit weighted LOO"
```

---

## Task 5: Contaminated-day exclusion mask (`exclusions.py`)

**Files:**
- Create: `apps/worker/worker/exclusions.py`
- Test: `apps/worker/tests/test_exclusions.py`

**Interfaces:**
- Produces: `contaminated_days(conn, symbols: list[str], start: date, end: date) -> dict[str, set[date]]` — earnings days (from `events`) plus index-event days (from `index_events`, empty until curated), per symbol.

- [ ] **Step 1: Write the failing test**

Create `apps/worker/tests/test_exclusions.py`:

```python
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.exclusions import contaminated_days


def test_earnings_day_is_flagged_index_empty_is_noop(db_conn: Connection) -> None:
    db_conn.execute(text(
        "INSERT INTO events (symbol, event_type, occurs_at) "
        "VALUES ('ZZZ', 'earnings', :d)"
    ), {"d": date(2020, 3, 2)})
    mask = contaminated_days(db_conn, ["ZZZ"], date(2020, 1, 1), date(2020, 6, 30))
    assert mask.get("ZZZ") == {date(2020, 3, 2)}
    # A symbol with no events and empty index_events contributes nothing.
    assert contaminated_days(db_conn, ["QQQ"], date(2020, 1, 1), date(2020, 6, 30)) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/worker && uv run pytest tests/test_exclusions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.exclusions'` (or DB-skip if `DATABASE_URL` unset; set it to run this task's test).

- [ ] **Step 3: Implement `exclusions.py`**

Create `apps/worker/worker/exclusions.py`:

```python
"""Contaminated-day exclusion mask (M12): days excluded from the FIT sample but
still scored, because they corrupt beta while being precisely the days worth
measuring. Earnings come from `events`; index-reconstitution from `index_events`
(ships empty, curated point-in-time). Ex-div needs no exclusion — returns are
computed from adj_c, which already removes the gap (spec)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

_EARNINGS = text("""
    SELECT symbol, occurs_at AS d FROM events
    WHERE event_type = 'earnings'
      AND symbol = ANY(:syms) AND occurs_at BETWEEN :start AND :end
""")

_INDEX = text("""
    SELECT symbol, trade_date AS d FROM index_events
    WHERE symbol = ANY(:syms) AND trade_date BETWEEN :start AND :end
""")


def contaminated_days(
    conn: Connection, symbols: list[str], start: date, end: date
) -> dict[str, set[date]]:
    out: dict[str, set[date]] = {}
    params = {"syms": symbols, "start": start, "end": end}
    for sql in (_EARNINGS, _INDEX):
        for row in conn.execute(sql, params).mappings():
            out.setdefault(row["symbol"], set()).add(row["d"])
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/worker && DATABASE_URL=$DATABASE_URL uv run pytest tests/test_exclusions.py -q`
Expected: PASS (needs `index_events` from migration `0010`; if Task 6 not yet applied, run after Task 6 — see note). Interim: this test passes only once `0010` is applied.

Note: `index_events` is created in Task 6. If you execute strictly in order, apply Task 6's migration before running this test. The module and its earnings assertion do not depend on Task 6; only the `_INDEX` query's table does.

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/exclusions.py && uv run ruff check worker/exclusions.py
git add apps/worker/worker/exclusions.py apps/worker/tests/test_exclusions.py
git commit -m "feat(m12): contaminated-day exclusion mask (earnings + index seam)"
```

---

## Task 6: Migration `0010_attribution_econometrics`

**Files:**
- Create: `apps/worker/alembic/versions/0010_attribution_econometrics.py`
- Test: `apps/worker/tests/test_attribution_migration.py`

**Interfaces:**
- Produces schema: `attribution.resid_z`, `basket_returns.weights`, tables `basket_loo_returns`, `index_events`, `attribution_signals`, `theme_dispersion`.

- [ ] **Step 1: Write the failing test**

Append to `apps/worker/tests/test_attribution_migration.py`:

```python
def test_m12_econometrics_schema_present_and_shared(db_conn: Connection) -> None:
    for table in ("basket_loo_returns", "index_events",
                  "attribution_signals", "theme_dispersion"):
        db_conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))  # exists

    attr_cols = db_conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='attribution'"
    )).scalars().all()
    assert "resid_z" in attr_cols

    basket_cols = db_conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='basket_returns'"
    )).scalars().all()
    assert "weights" in basket_cols

    # Shared reference data: no user_id on the new tables.
    for table in ("basket_loo_returns", "attribution_signals", "theme_dispersion"):
        cols = db_conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ), {"t": table}).scalars().all()
        assert "user_id" not in cols
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/worker && uv run pytest tests/test_attribution_migration.py::test_m12_econometrics_schema_present_and_shared -q`
Expected: FAIL — the tables/columns do not exist yet (or DB-skip without `DATABASE_URL`).

- [ ] **Step 3: Write the migration**

Create `apps/worker/alembic/versions/0010_attribution_econometrics.py`:

```python
"""M12 attribution econometrics: MAD salience, capped-LOO storage, index-event
seam, and derived-signal tables. All shared reference data (no user_id), RLS on
with a public read policy — the bars_daily/attribution precedent (0009/D21).

Revision ID: 0010_attribution_econometrics
Revises: 0009_attribution
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_attribution_econometrics"
down_revision: str | None = "0009_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE attribution    ADD COLUMN resid_z numeric;
        ALTER TABLE basket_returns ADD COLUMN weights jsonb;

        CREATE TABLE basket_loo_returns (
            theme_id        uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            excluded_symbol text NOT NULL,
            trade_date      date NOT NULL,
            model_version   integer NOT NULL,
            ret             numeric NOT NULL,
            n_members       integer NOT NULL,
            PRIMARY KEY (theme_id, excluded_symbol, trade_date, model_version)
        );

        CREATE TABLE index_events (
            symbol         text NOT NULL,
            trade_date     date NOT NULL,
            index_key      text NOT NULL,
            effective_from date NOT NULL,
            effective_to   date,
            PRIMARY KEY (symbol, trade_date, index_key)
        );

        CREATE TABLE attribution_signals (
            symbol         text NOT NULL,
            trade_date     date NOT NULL,
            model_version  integer NOT NULL,
            beta_drift_20d numeric,
            resid_momentum numeric,
            rolling_alpha  numeric,
            PRIMARY KEY (symbol, trade_date, model_version)
        );

        CREATE TABLE theme_dispersion (
            theme_id       uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            trade_date     date NOT NULL,
            model_version  integer NOT NULL,
            dispersion_mad numeric NOT NULL,
            PRIMARY KEY (theme_id, trade_date, model_version)
        );
    """)

    op.execute("""
        ALTER TABLE basket_loo_returns   ENABLE ROW LEVEL SECURITY;
        ALTER TABLE index_events         ENABLE ROW LEVEL SECURITY;
        ALTER TABLE attribution_signals  ENABLE ROW LEVEL SECURITY;
        ALTER TABLE theme_dispersion     ENABLE ROW LEVEL SECURITY;

        CREATE POLICY basket_loo_returns_read  ON basket_loo_returns  FOR SELECT USING (true);
        CREATE POLICY index_events_read        ON index_events        FOR SELECT USING (true);
        CREATE POLICY attribution_signals_read ON attribution_signals FOR SELECT USING (true);
        CREATE POLICY theme_dispersion_read    ON theme_dispersion    FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS theme_dispersion;
        DROP TABLE IF EXISTS attribution_signals;
        DROP TABLE IF EXISTS index_events;
        DROP TABLE IF EXISTS basket_loo_returns;
        ALTER TABLE basket_returns DROP COLUMN IF EXISTS weights;
        ALTER TABLE attribution    DROP COLUMN IF EXISTS resid_z;
    """)
```

- [ ] **Step 4: Apply and verify reversibility**

Run: `cd apps/worker && uv run alembic upgrade head && uv run alembic downgrade 0009_attribution && uv run alembic upgrade head`
Expected: all three succeed; `alembic current` shows `0010_attribution_econometrics`.

- [ ] **Step 5: Run the migration test**

Run: `cd apps/worker && uv run pytest tests/test_attribution_migration.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/worker/alembic/versions/0010_attribution_econometrics.py apps/worker/tests/test_attribution_migration.py
git commit -m "feat(m12): migration 0010 — resid_z, capped-LOO, index/signal tables"
```

---

## Task 7: Rewire `refit()` — two-stage fit, exclusion, weighted LOO, diagnostics

**Files:**
- Modify: `apps/worker/worker/attribution.py` (the `refit` DB function and its queries/upserts)
- Test: `apps/worker/tests/test_attribution.py` (DB integration)

**Interfaces:**
- Consumes: `fit_two_stage`, `TwoStageFit`, `baskets.{screen_and_cap, weighted_return, loo_weighted_return, upsert_basket_loo_return}`, `exclusions.contaminated_days`.
- Produces: `refit(conn, fit_date, *, now_utc, model_version) -> RefitResult` (signature unchanged), writing `attribution_fits` with `resid_scale = MAD` and a populated `diagnostics` jsonb, plus `basket_returns.weights` and `basket_loo_returns`.

- [ ] **Step 1: Add the liquidity read and diagnostics-aware fit upsert**

In `apps/worker/worker/attribution.py`, add a JSON import at the top: `import json`. Add a liquidity query beside `_WINDOW_RETURNS`:

```python
_WINDOW_LIQUIDITY = text("""
    SELECT symbol, session_date, (v * c) AS dv
    FROM bars_daily
    WHERE symbol = ANY(:syms) AND session_date > :start AND session_date <= :fit_date
""")
```

Replace `_UPSERT_FIT` (write `diagnostics` from a bound param and store MAD `resid_scale`):

```python
_UPSERT_FIT = text("""
    INSERT INTO attribution_fits
      (symbol, model_version, fit_date, window_start, window_end,
       beta_market, beta_theme, alpha, r2, resid_scale, n_obs, cold_start, diagnostics)
    VALUES (:symbol, :mv, :fit_date, :window_start, :window_end,
       :beta_market, :beta_theme, :alpha, :r2, :resid_scale, :n_obs, :cold_start,
       CAST(:diagnostics AS jsonb))
    ON CONFLICT (symbol, model_version, fit_date) DO UPDATE SET
       window_start=EXCLUDED.window_start, window_end=EXCLUDED.window_end,
       beta_market=EXCLUDED.beta_market, beta_theme=EXCLUDED.beta_theme,
       alpha=EXCLUDED.alpha, r2=EXCLUDED.r2, resid_scale=EXCLUDED.resid_scale,
       n_obs=EXCLUDED.n_obs, cold_start=EXCLUDED.cold_start, diagnostics=EXCLUDED.diagnostics
""")
```

Also update `_UPSERT_BASKET` in `baskets.py` to carry `weights` — replace it:

```python
_UPSERT_BASKET = text("""
    INSERT INTO basket_returns
        (theme_id, trade_date, model_version, ret, n_members, synthetic, revised, weights)
    VALUES (:theme_id, :trade_date, :model_version, :ret, :n_members, :synthetic, :revised,
            CAST(:weights AS jsonb))
    ON CONFLICT (theme_id, trade_date, model_version) DO UPDATE
        SET ret = EXCLUDED.ret, n_members = EXCLUDED.n_members,
            synthetic = EXCLUDED.synthetic, revised = EXCLUDED.revised,
            weights = EXCLUDED.weights
""")
```

And update `upsert_basket_return` in `baskets.py` to accept and pass weights:

```python
def upsert_basket_return(
    conn: Connection, theme_id: str, trade_date: date, model_version: int,
    br: BasketReturn, *, synthetic: bool, revised: bool,
    weights: dict[str, float] | None = None,
) -> None:
    import json
    conn.execute(_UPSERT_BASKET, {
        "theme_id": theme_id, "trade_date": trade_date, "model_version": model_version,
        "ret": br.ret, "n_members": br.n_members, "synthetic": synthetic, "revised": revised,
        "weights": json.dumps(weights) if weights is not None else None,
    })
```

- [ ] **Step 2: Rewrite the `refit` body**

Replace the entire `refit` function in `apps/worker/worker/attribution.py` with:

```python
def refit(
    conn: Connection, fit_date: date, *, now_utc: datetime, model_version: int
) -> RefitResult:
    """Fit each scored symbol's two-stage orthogonalized model over its trailing
    FIT_WINDOW *fit-eligible* sessions (contaminated days excluded), on its
    is_primary theme's capped/liquidity-screened leave-one-out basket. Persists
    attribution_fits (with diagnostics + MAD resid_scale), basket_returns (full
    weighted series + weights), and basket_loo_returns. Injected clock; symbols
    with no theme or too few observations are skipped, not fatal."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}
    scored = _scored_universe(conn, fit_date, member_symbols)

    needed = sorted(set(scored) | member_symbols | {BENCHMARK_SYMBOL})
    returns = _read_window_returns(conn, needed, fit_date)
    liquidity = _read_window_liquidity(conn, needed, fit_date)
    market = returns.get(BENCHMARK_SYMBOL, {})

    start = fit_date - timedelta(days=_LOOKBACK_DAYS)
    contaminated = contaminated_days(conn, sorted(scored), start, fit_date)

    needed_themes = {
        t for s in scored if (t := primary_theme_of(by_theme, s, fit_date)) is not None
    }

    # Full capped/screened basket per theme per day; persisted with its weights.
    basket_full: dict[str, dict[date, tuple[float, int]]] = {}
    for tid in needed_themes:
        members = by_theme[tid]
        day_map: dict[date, tuple[float, int]] = {}
        days = {d for m in members for d in returns.get(m.symbol, {})}
        for day in days:
            live = members_on(members, day)
            rets = {s: returns[s][day] for s in live if day in returns.get(s, {})}
            liq = {s: liquidity.get(s, {}).get(day, 0.0) for s in live}
            weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
            if not weights or not rets:
                continue
            br = weighted_return(rets, weights)
            day_map[day] = (br.ret, br.n_members)
            upsert_basket_return(
                conn, tid, day, model_version, br,
                synthetic=False, revised=False, weights=weights,
            )
        basket_full[tid] = day_map

    pending: list[tuple[str, str, TwoStageFit, date, date]] = []
    skipped: list[str] = []
    noncold: dict[str, list[float]] = {}
    for symbol in scored:
        theme_id = primary_theme_of(by_theme, symbol, fit_date)
        if theme_id is None:
            skipped.append(symbol)
            continue
        members = by_theme[theme_id]
        r_sym = returns.get(symbol, {})
        bad = contaminated.get(symbol, set())

        # Fit-eligible common days: symbol, market, and a basket value all present,
        # minus the symbol's contaminated days. Then take the trailing window.
        common = sorted(
            d for d in (set(r_sym) & set(market) & set(basket_full.get(theme_id, {})))
            if d not in bad
        )[-FIT_WINDOW:]
        if len(common) < 2:
            skipped.append(symbol)
            continue

        r_x, r_m, r_t = [], [], []
        for day in common:
            live = members_on(members, day)
            rets = {s: returns[s][day] for s in live if day in returns.get(s, {})}
            liq = {s: liquidity.get(s, {}).get(day, 0.0) for s in live}
            if symbol in rets:
                loo = loo_weighted_return(
                    rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME,
                    cap=BASKET_CAP, excluded=symbol,
                )
            else:
                weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
                loo = weighted_return(rets, weights)
            r_x.append(r_sym[day]); r_m.append(market[day]); r_t.append(loo.ret)
            upsert_basket_loo_return(conn, theme_id, symbol, day, model_version, loo)

        fit = fit_two_stage(r_x, r_m, r_t)
        pending.append((symbol, theme_id, fit, common[0], common[-1]))
        if not fit.cold_start:
            noncold.setdefault(theme_id, []).append(fit.beta_theta)

    written = 0
    for symbol, theme_id, fit, w_start, w_end in pending:
        beta_theta = fit.beta_theta
        if fit.cold_start:
            medians = noncold.get(theme_id, [])
            median_beta = statistics.median(medians) if medians else 0.0
            w = min(1.0, fit.n_obs / FIT_WINDOW)
            beta_theta = w * fit.beta_theta + (1.0 - w) * median_beta
        diagnostics = {
            "a": fit.a, "b": fit.b,
            "beta_se": list(fit.beta_se),
            "resid_autocorr": fit.durbin_watson,
            "cond_number": fit.cond_number,
            "huber_converged": fit.huber_converged,
            "r2_collapsed": fit.r2_collapsed,
        }
        conn.execute(_UPSERT_FIT, {
            "symbol": symbol, "mv": model_version, "fit_date": fit_date,
            "window_start": w_start, "window_end": w_end,
            "beta_market": _q(fit.beta_market), "beta_theme": _q(beta_theta),
            "alpha": _q(fit.alpha), "r2": _q(fit.r2), "resid_scale": _q(fit.resid_scale),
            "n_obs": fit.n_obs, "cold_start": fit.cold_start,
            "diagnostics": json.dumps(diagnostics),
        })
        written += 1

    return RefitResult(fits_written=written, symbols=[p[0] for p in pending], skipped=skipped)
```

Add the imports this uses to the top-of-file import block from `worker.baskets` (extend the existing import): add `screen_and_cap`, `weighted_return`, `loo_weighted_return`, `upsert_basket_loo_return`, `BASKET_CAP`, `MIN_DOLLAR_VOLUME`. Add `from worker.exclusions import contaminated_days`.

Add the liquidity reader beside `_read_window_returns`:

```python
def _read_window_liquidity(
    conn: Connection, symbols: list[str], fit_date: date
) -> dict[str, dict[date, float]]:
    start = fit_date - timedelta(days=_LOOKBACK_DAYS)
    out: dict[str, dict[date, float]] = {}
    rows = conn.execute(
        _WINDOW_LIQUIDITY, {"syms": symbols, "fit_date": fit_date, "start": start}
    ).mappings()
    for row in rows:
        out.setdefault(row["symbol"], {})[row["session_date"]] = float(row["dv"])
    return out
```

- [ ] **Step 2b: Delete the now-dead M11 helpers**

Remove `fit_ols` and `apply_cold_start` **only if** no remaining code or test imports them. `test_attribution.py` imports `fit_ols`/`apply_cold_start` for M11 pure tests — keep both functions and those tests (they still validate the primitives and the plain-OLS path referenced in the spec). Do not delete.

- [ ] **Step 3: Write the integration test**

Append to `apps/worker/tests/test_attribution.py`:

```python
from datetime import UTC, date, datetime

from sqlalchemy import text as _text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for


def _seed_theme(conn: Connection, key: str, symbols: list[str]) -> str:
    tid = conn.execute(_text(
        "INSERT INTO themes (key, name) VALUES (:k, :k) RETURNING id::text"
    ), {"k": key}).scalar_one()
    for s in symbols:
        conn.execute(_text(
            "INSERT INTO theme_members (theme_id, symbol, effective_from) "
            "VALUES (:t, :s, :d)"
        ), {"t": tid, "s": s, "d": date(2019, 1, 1)})
    return tid


def _hold(conn: Connection, symbol: str) -> None:
    from worker.constants import DEV_USER_ID
    hid = conn.execute(_text(
        "INSERT INTO holdings (user_id, symbol) VALUES (:u, :s) RETURNING id"
    ), {"u": DEV_USER_ID, "s": symbol}).scalar_one()
    conn.execute(_text(
        "INSERT INTO lots (holding_id, qty, cost_basis_cents, opened_on) "
        "VALUES (:h, 100, 100000, :d)"
    ), {"h": hid, "d": date(2019, 1, 1)})


def test_refit_writes_diagnostics_and_loo_and_beats_joint_condition(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "semis", ["AAA", "BBB", "CCC", "DDD"])
    _hold(db_conn, "AAA")

    from worker.attribution import refit
    res = refit(db_conn, date(2020, 6, 30), now_utc=datetime(2020, 6, 30, tzinfo=UTC),
                model_version=2)
    assert "AAA" in res.symbols

    diag = db_conn.execute(_text(
        "SELECT diagnostics FROM attribution_fits "
        "WHERE symbol='AAA' AND model_version=2 AND fit_date='2020-06-30'"
    )).scalar_one()
    assert {"a", "b", "beta_se", "cond_number", "huber_converged"} <= set(diag)

    loo_n = db_conn.execute(_text(
        "SELECT count(*) FROM basket_loo_returns "
        "WHERE excluded_symbol='AAA' AND model_version=2"
    )).scalar_one()
    assert loo_n > 0
```

- [ ] **Step 4: Run the integration test**

Run: `cd apps/worker && uv run pytest tests/test_attribution.py -q`
Expected: PASS (DB tests run when `DATABASE_URL` is set; pure tests always run).

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/attribution.py worker/baskets.py && uv run ruff check worker/attribution.py worker/baskets.py
git add apps/worker/worker/attribution.py apps/worker/worker/baskets.py apps/worker/tests/test_attribution.py
git commit -m "feat(m12): refit uses two-stage fit, weighted LOO, exclusion, diagnostics"
```

---

## Task 8: Rewire `score()` — reconstruct rho, write `resid_z`

**Files:**
- Modify: `apps/worker/worker/attribution.py` (the `score` DB function, `_LATEST_FITS`, `_UPSERT_ATTR`)
- Test: `apps/worker/tests/test_attribution.py`

**Interfaces:**
- Consumes: stored stage-A `(a, b)` and `resid_scale` from `attribution_fits`; `baskets.{screen_and_cap, weighted_return, loo_weighted_return}`.
- Produces: `score(...)` (signature unchanged) writing `attribution.resid_z = resid_bps / resid_scale`, decomposed via `decompose_ortho`.

- [ ] **Step 1: Extend the fit read and the attribution upsert**

Replace `_LATEST_FITS` to also return `alpha`, `resid_scale`, and `diagnostics`:

```python
_LATEST_FITS = text("""
    SELECT DISTINCT ON (symbol)
           symbol, beta_market, beta_theme, alpha, r2, resid_scale, n_obs,
           cold_start, diagnostics
    FROM attribution_fits
    WHERE model_version = :mv AND fit_date <= :d
    ORDER BY symbol, fit_date DESC
""")
```

Replace `_UPSERT_ATTR` to include `resid_z`:

```python
_UPSERT_ATTR = text("""
    INSERT INTO attribution
      (symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps,
       resid_z, beta_market, beta_theme, r2, n_obs, provisional, cold_start,
       synthetic, revised, computed_at)
    VALUES (:symbol, :trade_date, :mv, :market_bps, :theme_bps, :resid_bps, :total_bps,
       :resid_z, :beta_market, :beta_theme, :r2, :n_obs, :provisional, :cold_start,
       :synthetic, :revised, :computed_at)
    ON CONFLICT (symbol, trade_date, model_version) DO UPDATE SET
       market_bps=EXCLUDED.market_bps, theme_bps=EXCLUDED.theme_bps,
       resid_bps=EXCLUDED.resid_bps, total_bps=EXCLUDED.total_bps, resid_z=EXCLUDED.resid_z,
       beta_market=EXCLUDED.beta_market, beta_theme=EXCLUDED.beta_theme, r2=EXCLUDED.r2,
       n_obs=EXCLUDED.n_obs, provisional=EXCLUDED.provisional, cold_start=EXCLUDED.cold_start,
       synthetic=EXCLUDED.synthetic, revised=EXCLUDED.revised, computed_at=EXCLUDED.computed_at
""")
```

Add a day-liquidity query beside `_DAY_RETURNS`:

```python
_DAY_LIQUIDITY = text("""
    SELECT symbol, (v * c) AS dv FROM bars_daily
    WHERE symbol = ANY(:syms) AND session_date = :d
""")
```

- [ ] **Step 2: Rewrite the `score` body**

Replace the entire `score` function with:

```python
def score(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int,
    synthetic: bool,
) -> ScoreResult:
    """Decompose each scored name's move on trade_date using its latest fit's
    stored stage-A (a, b) to reconstruct rho, and its is_primary theme's
    capped/screened leave-one-out basket. Writes resid_z = resid_bps / resid_scale
    (the MAD-scaled salience score M13 ranks on). Contaminated days are NOT
    excluded here — they are scored (the residual is the point)."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}

    fits = {
        row["symbol"]: row
        for row in conn.execute(_LATEST_FITS, {"mv": model_version, "d": trade_date}).mappings()
    }
    needed = sorted(set(fits) | member_symbols | {BENCHMARK_SYMBOL})
    day_returns = {
        row["symbol"]: float(row["ret"])
        for row in conn.execute(_DAY_RETURNS, {"syms": needed, "d": trade_date}).mappings()
    }
    day_liquidity = {
        row["symbol"]: float(row["dv"])
        for row in conn.execute(_DAY_LIQUIDITY, {"syms": needed, "d": trade_date}).mappings()
    }
    market_ret = day_returns.get(BENCHMARK_SYMBOL)

    written = 0
    skipped: list[str] = []
    for symbol, frow in fits.items():
        r_x = day_returns.get(symbol)
        theme_id = primary_theme_of(by_theme, symbol, trade_date)
        if r_x is None or market_ret is None or theme_id is None:
            skipped.append(symbol)
            continue
        members = by_theme[theme_id]
        live = members_on(members, trade_date)
        rets = {s: day_returns[s] for s in live if s in day_returns}
        liq = {s: day_liquidity.get(s, 0.0) for s in live}
        if symbol in rets:
            loo = loo_weighted_return(
                rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP, excluded=symbol,
            )
        else:
            weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
            if not weights:
                skipped.append(symbol)
                continue
            loo = weighted_return(rets, weights)

        diag = frow["diagnostics"] or {}
        fit = TwoStageFit(
            alpha=float(frow["alpha"]) if frow["alpha"] is not None else 0.0,
            beta_market=float(frow["beta_market"]),
            beta_theta=float(frow["beta_theme"]),
            a=float(diag.get("a", 0.0)), b=float(diag.get("b", 0.0)),
            r2=float(frow["r2"]) if frow["r2"] is not None else None,
            resid_scale=float(frow["resid_scale"]) if frow["resid_scale"] is not None else None,
            beta_se=(0.0, 0.0, 0.0), durbin_watson=0.0, cond_number=0.0,
            huber_converged=True, r2_collapsed=False,
            n_obs=int(frow["n_obs"]), cold_start=bool(frow["cold_start"]),
        )
        d = decompose_ortho(fit, market_ret, loo.ret, r_x)
        resid_z = None
        if fit.resid_scale not in (None, 0.0):
            resid_z = d.resid_bps / fit.resid_scale
        conn.execute(_UPSERT_ATTR, {
            "symbol": symbol, "trade_date": trade_date, "mv": model_version,
            "market_bps": _q(d.market_bps), "theme_bps": _q(d.theme_bps),
            "resid_bps": _q(d.resid_bps), "total_bps": _q(d.total_bps), "resid_z": _q(resid_z),
            "beta_market": _q(fit.beta_market), "beta_theme": _q(fit.beta_theta),
            "r2": _q(fit.r2), "n_obs": fit.n_obs,
            "provisional": synthetic, "cold_start": fit.cold_start,
            "synthetic": synthetic, "revised": False, "computed_at": now_utc,
        })
        written += 1

    return ScoreResult(rows_written=written, skipped=skipped)
```

Note: `score` no longer writes daily `basket_returns` (that belonged to M11's equal-weight path). If `reconcile.py` reads `basket_returns` for the scored day, leave a weighted daily basket write in place — verify against `reconcile.py`; if it does, insert `upsert_basket_return(conn, theme_id, trade_date, model_version, weighted_return(rets, screen_and_cap(...)), synthetic=synthetic, revised=False, weights=...)` before the per-symbol loop, keyed per theme. Confirm by reading `worker/reconcile.py` at execution time.

- [ ] **Step 3: Write the test**

Append to `apps/worker/tests/test_attribution.py`:

```python
def test_score_writes_resid_z_and_additive_rows(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "semis", ["AAA", "BBB", "CCC", "DDD"])
    _hold(db_conn, "AAA")

    from worker.attribution import refit, score
    now = datetime(2020, 6, 30, tzinfo=UTC)
    refit(db_conn, date(2020, 6, 30), now_utc=now, model_version=2)
    res = score(db_conn, date(2020, 6, 30), now_utc=now, model_version=2, synthetic=False)
    assert res.rows_written >= 1

    row = db_conn.execute(_text(
        "SELECT market_bps, theme_bps, resid_bps, total_bps, resid_z FROM attribution "
        "WHERE symbol='AAA' AND model_version=2 AND trade_date='2020-06-30'"
    )).mappings().one()
    assert row["resid_z"] is not None
    got = float(row["market_bps"]) + float(row["theme_bps"]) + float(row["resid_bps"])
    assert got == pytest.approx(float(row["total_bps"]), abs=1e-6)
```

- [ ] **Step 4: Run the test**

Run: `cd apps/worker && uv run pytest tests/test_attribution.py -k "score_writes_resid_z" -q`
Expected: PASS.

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/attribution.py && uv run ruff check worker/attribution.py
git add apps/worker/worker/attribution.py apps/worker/tests/test_attribution.py
git commit -m "feat(m12): score reconstructs rho from stored (a,b), writes resid_z"
```

---

## Task 9: Derived signals (`attribution_signals.py`) + CLI

**Files:**
- Create: `apps/worker/worker/attribution_signals.py`
- Modify: `apps/worker/worker/cli.py` (add `attribution signals --date`)
- Test: `apps/worker/tests/test_attribution_signals.py`

**Interfaces:**
- Produces: `compute_signals(conn, trade_date, *, now_utc, model_version) -> SignalsResult(names_written: int, themes_written: int)`, writing `attribution_signals` and `theme_dispersion`.

- [ ] **Step 1: Write the failing test**

Create `apps/worker/tests/test_attribution_signals.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for
from tests.test_attribution import _hold, _seed_theme


def test_signals_and_dispersion_persist(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "semis", ["AAA", "BBB", "CCC", "DDD"])
    _hold(db_conn, "AAA")

    from worker.attribution import refit, score
    from worker.attribution_signals import compute_signals
    now = datetime(2020, 6, 30, tzinfo=UTC)
    refit(db_conn, date(2020, 6, 30), now_utc=now, model_version=2)
    score(db_conn, date(2020, 6, 30), now_utc=now, model_version=2, synthetic=False)
    res = compute_signals(db_conn, date(2020, 6, 30), now_utc=now, model_version=2)
    assert res.names_written >= 1

    disp = db_conn.execute(text(
        "SELECT dispersion_mad FROM theme_dispersion WHERE model_version=2"
    )).scalars().all()
    assert disp and all(d is not None for d in disp)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/worker && uv run pytest tests/test_attribution_signals.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.attribution_signals'`.

- [ ] **Step 3: Implement `attribution_signals.py`**

Create `apps/worker/worker/attribution_signals.py`:

```python
"""Derived attribution signals (M12, Layer 3), computed weekly from stored fits
and residuals — cheap over data already persisted. Feed M13 consumers; computed
here as part of the scoring machinery. Float, off the money path; quantized at
storage via attribution._q."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.attribution import _q
from worker.baskets import read_theme_members
from worker.baskets import members_on, primary_theme_of

BETA_DRIFT_FITS = 4          # ~20 sessions of weekly fits
RESID_TRAIL = 20             # sessions for rolling alpha / momentum

_FIT_HISTORY = text("""
    SELECT symbol, fit_date, beta_theme FROM attribution_fits
    WHERE model_version = :mv AND fit_date <= :d
    ORDER BY symbol, fit_date DESC
""")

_RESID_TRAIL = text("""
    SELECT symbol, trade_date, resid_bps FROM attribution
    WHERE model_version = :mv AND trade_date <= :d
    ORDER BY symbol, trade_date DESC
""")

_DAY_RESID = text("""
    SELECT symbol, resid_bps FROM attribution
    WHERE model_version = :mv AND trade_date = :d
""")

_UPSERT_SIGNAL = text("""
    INSERT INTO attribution_signals
        (symbol, trade_date, model_version, beta_drift_20d, resid_momentum, rolling_alpha)
    VALUES (:symbol, :d, :mv, :drift, :momentum, :alpha)
    ON CONFLICT (symbol, trade_date, model_version) DO UPDATE SET
        beta_drift_20d=EXCLUDED.beta_drift_20d, resid_momentum=EXCLUDED.resid_momentum,
        rolling_alpha=EXCLUDED.rolling_alpha
""")

_UPSERT_DISPERSION = text("""
    INSERT INTO theme_dispersion (theme_id, trade_date, model_version, dispersion_mad)
    VALUES (:theme_id, :d, :mv, :mad)
    ON CONFLICT (theme_id, trade_date, model_version) DO UPDATE SET
        dispersion_mad=EXCLUDED.dispersion_mad
""")


@dataclass(frozen=True)
class SignalsResult:
    names_written: int
    themes_written: int


def _mad(xs: list[float]) -> float:
    med = statistics.median(xs)
    return 1.4826 * statistics.median([abs(x - med) for x in xs])


def compute_signals(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int
) -> SignalsResult:
    beta_hist: dict[str, list[float]] = {}
    for row in conn.execute(_FIT_HISTORY, {"mv": model_version, "d": trade_date}).mappings():
        beta_hist.setdefault(row["symbol"], []).append(float(row["beta_theme"]))

    resid_hist: dict[str, list[float]] = {}
    for row in conn.execute(_RESID_TRAIL, {"mv": model_version, "d": trade_date}).mappings():
        resid_hist.setdefault(row["symbol"], []).append(float(row["resid_bps"]))

    names = 0
    for symbol in sorted(set(beta_hist) | set(resid_hist)):
        betas = beta_hist.get(symbol, [])
        drift = betas[0] - betas[BETA_DRIFT_FITS] if len(betas) > BETA_DRIFT_FITS else None
        trail = resid_hist.get(symbol, [])[:RESID_TRAIL]
        alpha = statistics.fmean(trail) if trail else None
        momentum = statistics.fmean(trail[:5]) if len(trail) >= 5 else None
        conn.execute(_UPSERT_SIGNAL, {
            "symbol": symbol, "d": trade_date, "mv": model_version,
            "drift": _q(drift), "momentum": _q(momentum), "alpha": _q(alpha),
        })
        names += 1

    by_theme = read_theme_members(conn)
    day_resid = {
        row["symbol"]: float(row["resid_bps"])
        for row in conn.execute(_DAY_RESID, {"mv": model_version, "d": trade_date}).mappings()
    }
    themes = 0
    for theme_id, members in by_theme.items():
        live = members_on(members, trade_date)
        vals = [day_resid[s] for s in live if s in day_resid]
        if len(vals) < 2:
            continue
        conn.execute(_UPSERT_DISPERSION, {
            "theme_id": theme_id, "d": trade_date, "mv": model_version, "mad": _q(_mad(vals)),
        })
        themes += 1

    return SignalsResult(names_written=names, themes_written=themes)
```

- [ ] **Step 4: Wire the CLI subcommand**

In `apps/worker/worker/cli.py`, after the `a_recon` parser block, add:

```python
    a_signals = attr_sub.add_parser("signals", help="weekly derived signals + theme dispersion")
    a_signals.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
```

In `_attribution`, before the final `raise SystemExit(...)`, add:

```python
    if attr_command == "signals":
        from worker.attribution_signals import compute_signals

        with engine.begin() as conn:
            sig = compute_signals(conn, trade_date, now_utc=now,
                                  model_version=ATTRIBUTION_MODEL_VERSION)
        print(f"signals: {sig.names_written} name(s), {sig.themes_written} theme(s)")
        return
```

Update the final `raise SystemExit(...)` help string to include `signals`:

```python
    raise SystemExit("unknown attribution subcommand; try themes-seed|refit|score|reconcile|signals")
```

- [ ] **Step 5: Run the test**

Run: `cd apps/worker && uv run pytest tests/test_attribution_signals.py -q`
Expected: PASS.

- [ ] **Step 6: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker/attribution_signals.py worker/cli.py && uv run ruff check worker/attribution_signals.py worker/cli.py
git add apps/worker/worker/attribution_signals.py apps/worker/worker/cli.py apps/worker/tests/test_attribution_signals.py
git commit -m "feat(m12): derived signals (beta drift, momentum, alpha, dispersion) + CLI"
```

---

## Task 10: Bump model version to 2 + version coexistence + stability

**Files:**
- Modify: `apps/worker/worker/constants.py`
- Modify: `apps/worker/tests/test_attribution_snapshot.py` (regenerate for v2)
- Create: `apps/worker/tests/test_attribution_stability.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Bump the constant**

In `apps/worker/worker/constants.py`, change `ATTRIBUTION_MODEL_VERSION = 1` to `ATTRIBUTION_MODEL_VERSION = 2` and update the comment to note v1 (M11) stays readable.

- [ ] **Step 2: Write the version-coexistence + stability tests**

Create `apps/worker/tests/test_attribution_stability.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for
from tests.test_attribution import _hold, _seed_theme


def test_version_1_rows_are_untouched_by_a_version_2_run(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "semis", ["AAA", "BBB", "CCC", "DDD"])
    _hold(db_conn, "AAA")

    # A pre-existing v1 fit row (as M11 would have written).
    db_conn.execute(text(
        "INSERT INTO attribution_fits (symbol, model_version, fit_date, window_start, "
        "window_end, beta_market, beta_theme, alpha, n_obs) "
        "VALUES ('AAA', 1, '2020-06-30', '2020-01-01', '2020-06-30', 0.9, 0.4, 0.0, 120)"
    ))
    from worker.attribution import refit
    refit(db_conn, date(2020, 6, 30), now_utc=datetime(2020, 6, 30, tzinfo=UTC),
          model_version=2)
    v1 = db_conn.execute(text(
        "SELECT beta_theme FROM attribution_fits WHERE symbol='AAA' AND model_version=1"
    )).scalar_one()
    assert float(v1) == pytest.approx(0.4)  # unchanged


def test_beta_theta_is_stable_across_weekly_refits(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=200, end=date(2020, 6, 30))
    _seed_theme(db_conn, "semis", ["AAA", "BBB", "CCC", "DDD"])
    _hold(db_conn, "AAA")

    from worker.attribution import refit
    betas: list[float] = []
    for k in range(20):
        d = date(2020, 6, 30) - timedelta(days=7 * k)
        refit(db_conn, d, now_utc=datetime(2020, 6, 30, tzinfo=UTC), model_version=2)
        b = db_conn.execute(text(
            "SELECT beta_theme FROM attribution_fits "
            "WHERE symbol='AAA' AND model_version=2 AND fit_date=:d"
        ), {"d": d}).scalar_one()
        betas.append(float(b))
    assert max(betas) - min(betas) < 0.3  # spec stability bar
```

- [ ] **Step 3: Regenerate the snapshot for v2**

Run: `cd apps/worker && uv run pytest tests/test_attribution_snapshot.py -q`
Expected: the snapshot test FAILS (v2 numbers differ from v1). Inspect the diff, confirm it reflects the new estimator (not a bug), then update the frozen expected values per the file's existing regeneration mechanism (e.g., `--snapshot-update` if used, or edit the expected literals). Re-run to green.

- [ ] **Step 4: Run the full suite**

Run: `cd apps/worker && uv run pytest -q`
Expected: PASS (DB tests run with `DATABASE_URL` set; otherwise skipped).

- [ ] **Step 5: Typecheck, lint, commit**

```bash
cd apps/worker && uv run mypy worker && uv run ruff check worker
git add apps/worker/worker/constants.py apps/worker/tests/test_attribution_stability.py apps/worker/tests/test_attribution_snapshot.py
git commit -m "feat(m12): bump ATTRIBUTION_MODEL_VERSION=2; stability + coexistence tests"
```

---

## Self-Review

**Spec coverage:**
- Sequential orthogonalization → Task 3 (`orthogonalize`, `fit_two_stage`, `decompose_ortho`); stored `(a,b)` in diagnostics (Task 7) and reconstructed at score (Task 8). ✓
- Robust Huber IRLS → Task 1. ✓
- EWMA (~60d) → Task 1 (`ewma_weights`, composed in `irls_huber`), applied in `fit_two_stage` (Task 3). ✓
- MAD residual scaling + `resid_z` → Task 1 (`mad_scale`), stored as `resid_scale` (Task 7), `resid_z` at score (Task 8). ✓
- Contaminated-day exclusion (earnings real, ex-div via adj_c → none, index empty seam) → Task 5 + applied in `refit` (Task 7); scored-but-excluded verified structurally (score does not exclude, Task 8). ✓
- Capping + liquidity screen + explicit weighted LOO + `basket_loo_returns` + `basket_returns.weights` → Task 4 + Task 6 (schema) + Task 7 (materialize). ✓
- Diagnostics (β SE, resid autocorr/DW, condition number, orthogonalization {a,b}, huber_converged, r2_collapsed) → Task 2 + Task 3 + stored Task 7. ✓
- Derived signals (β drift, theme dispersion, rolling α, resid momentum) → Task 9. ✓
- Migration `0010` (split from `0011`/M12b) → Task 6. ✓
- Bump `ATTRIBUTION_MODEL_VERSION=2` + versioning coexistence → Task 10. ✓
- DoD: orthogonality (T3), stability (T10), MAD-vs-stdev (T1/T2 `mad_scale`), robustness (T1), additivity (T3/T8), LOO parity (T4), versioning (T10). ✓
- Out of scope (M12b, BriefObject, M13 consumers) → not in this plan, by design. ✓

**Placeholder scan:** No `TBD`/`TODO`/"handle edge cases"/"similar to Task N" remain; every code and test step carries its actual content.

**Type consistency:** `TwoStageFit` fields are identical where constructed in `fit_two_stage` (Task 3) and reconstructed in `score` (Task 8). `BasketReturn(ret, n_members)` unchanged from M11 and reused by `weighted_return`/`loo_weighted_return` (Task 4). `_q` (from `attribution`) reused in `attribution_signals` (Task 9). `contaminated_days` signature matches its call in `refit` (Task 7). CLI `signals` dispatch matches `compute_signals` signature (Task 9).

One consistency risk flagged for execution: **Task 8's note about `basket_returns` daily writes and `reconcile.py`.** Read `worker/reconcile.py` at execution time; if it depends on a daily `basket_returns` row, keep a weighted daily write in `score` as described. This is the only place a neighboring module could break.
