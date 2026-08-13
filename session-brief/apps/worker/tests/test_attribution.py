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
    return str(tid)


def _hold(conn: Connection, symbol: str) -> None:
    from worker.constants import DEV_USER_ID
    sector_id = conn.execute(_text(
        "INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"
    ), {"u": DEV_USER_ID, "n": symbol}).scalar_one()
    hid = conn.execute(_text(
        "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :sec, :s) RETURNING id"
    ), {"u": DEV_USER_ID, "sec": sector_id, "s": symbol}).scalar_one()
    conn.execute(_text(
        "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
        "VALUES (:u, :h, 100, 100000, :d)"
    ), {"u": DEV_USER_ID, "h": hid, "d": date(2019, 1, 1)})


def test_refit_writes_diagnostics_and_loo_and_beats_joint_condition(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "m12_semis_test", ["AAA", "BBB", "CCC", "DDD"])
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


def test_score_writes_resid_z_and_additive_rows(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "m12_semis_test2", ["AAA", "BBB", "CCC", "DDD"])
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
