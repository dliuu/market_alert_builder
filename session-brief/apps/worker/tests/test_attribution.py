from __future__ import annotations

import random

import numpy as np
import pytest

from worker.attribution import (
    COLD_START_FLOOR,
    apply_cold_start,
    decompose,
    decompose_ortho,
    fit_ols,
    fit_two_stage,
    orthogonalize,
)


def _series(n: int, seed: int) -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(seed)
    r_market = [rng.gauss(0, 0.01) for _ in range(n)]
    r_theme = [m + rng.gauss(0, 0.005) for m in r_market]  # ~correlated, as in spec
    r_x = [
        0.3 * m + 0.5 * t + rng.gauss(0, 0.004)
        for m, t in zip(r_market, r_theme, strict=True)
    ]
    return r_x, r_market, r_theme


def test_additivity_holds_exactly_over_messy_inputs() -> None:
    r_x, r_m, r_t = _series(120, seed=1)
    fit = fit_ols(r_x, r_m, r_t)
    for m, t, x in zip(r_m, r_t, r_x, strict=True):
        d = decompose(fit, m, t, x)
        assert d.market_bps + d.theme_bps + d.resid_bps == pytest.approx(d.total_bps, abs=1e-9)


def test_null_shuffled_calendar_flattens_the_residual_signal() -> None:
    # Shuffling r_x against the factors destroys the relationship: the fitted
    # betas shrink toward zero and residual variance approaches total variance.
    r_x, r_m, r_t = _series(120, seed=2)
    random.Random(99).shuffle(r_x)
    fit = fit_ols(r_x, r_m, r_t)
    assert abs(fit.beta_market) < 0.5 and abs(fit.beta_theme) < 0.5


def test_known_answer_earnings_gap_produces_a_large_residual() -> None:
    r_x, r_m, r_t = _series(120, seed=3)
    fit = fit_ols(r_x, r_m, r_t)
    # A +8% idiosyncratic gap on a flat-market, flat-theme day -> residual ~ the gap.
    d = decompose(fit, r_market=0.0, r_theme=0.0, r_x=0.08)
    assert d.resid_bps > 700  # ~800 bps, dominated by idiosyncratic


def test_cold_start_shrinks_toward_theme_median_and_flags() -> None:
    r_x, r_m, r_t = _series(COLD_START_FLOOR - 1, seed=4)
    fit = fit_ols(r_x, r_m, r_t)
    assert fit.cold_start is True
    shrunk = apply_cold_start(fit, theme_median_beta=0.4)
    w = (COLD_START_FLOOR - 1) / 120
    assert shrunk.beta_theme == pytest.approx(w * fit.beta_theme + (1 - w) * 0.4)


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
