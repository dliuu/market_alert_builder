from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for
from tests.test_attribution import _hold, _seed_theme


def test_signals_and_dispersion_persist(db_conn: Connection) -> None:
    syms = ["SPY", "AAA", "BBB", "CCC", "DDD"]
    seed_bars_for(db_conn, syms, sessions=160, end=date(2020, 6, 30))
    _seed_theme(db_conn, "m12_signals_semis", ["AAA", "BBB", "CCC", "DDD"])
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
