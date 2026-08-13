from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for
from worker.attribution import refit, score
from worker.constants import ATTRIBUTION_MODEL_VERSION as MV
from worker.themes_seed import seed_themes

SYMBOLS = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "SPY"]
D = date(2020, 6, 30)
NOW = datetime(2020, 6, 30, 22, 0, tzinfo=UTC)


def _rows(conn: Connection) -> list[dict[str, object]]:
    return [dict(r) for r in conn.execute(text("""
        SELECT symbol, market_bps, theme_bps, resid_bps, total_bps
        FROM attribution WHERE trade_date = :d AND model_version = :mv ORDER BY symbol
    """), {"d": D, "mv": MV}).mappings().all()]


def test_seeded_theme_120_days_snapshots_stably(db_conn: Connection) -> None:
    seed_themes(db_conn)
    seed_bars_for(db_conn, SYMBOLS, sessions=121)

    refit(db_conn, D, now_utc=NOW, model_version=MV)
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=False)
    first = _rows(db_conn)

    # Idempotent re-run reproduces identical rows (snapshot-stable).
    refit(db_conn, D, now_utc=NOW, model_version=MV)
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=False)
    second = _rows(db_conn)

    assert first == second
    assert first  # non-empty
