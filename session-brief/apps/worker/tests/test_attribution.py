from __future__ import annotations

import random

import pytest

from worker.attribution import (
    COLD_START_FLOOR,
    apply_cold_start,
    decompose,
    fit_ols,
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
