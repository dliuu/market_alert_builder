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
    _seed_theme(db_conn, "m12_stability_semis", ["AAA", "BBB", "CCC", "DDD"])
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
    _seed_theme(db_conn, "m12_stability_semis", ["AAA", "BBB", "CCC", "DDD"])
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
