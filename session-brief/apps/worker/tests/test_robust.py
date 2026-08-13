from __future__ import annotations

import numpy as np
import pytest

from worker.robust import (
    beta_standard_errors,
    condition_number,
    durbin_watson,
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
