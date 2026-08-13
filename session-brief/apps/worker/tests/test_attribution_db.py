from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from tests.helpers_attribution import seed_bars_for
from worker.attribution import refit, score
from worker.constants import ATTRIBUTION_MODEL_VERSION as MV
from worker.themes_seed import seed_themes

SYMBOLS = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "SPY"]
D = date(2020, 6, 30)
NOW = datetime(2020, 6, 30, 22, 0, tzinfo=UTC)


def test_refit_then_score_writes_additive_rows(db_conn):
    seed_themes(db_conn)
    seed_bars_for(db_conn, SYMBOLS, sessions=121)

    refit(db_conn, D, now_utc=NOW, model_version=MV)
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=False)

    rows = db_conn.execute(text("""
        SELECT symbol, market_bps, theme_bps, resid_bps, total_bps
        FROM attribution WHERE trade_date = :d AND model_version = :mv
    """), {"d": D, "mv": MV}).mappings().all()
    scored = {r["symbol"] for r in rows}
    # Every seeded semis name (a member with bars) is scored.
    assert {"NVDA", "AMD", "MU", "SNDK", "AVGO"} <= scored
    for r in rows:
        total = r["market_bps"] + r["theme_bps"] + r["resid_bps"]
        assert abs(total - r["total_bps"]) < Decimal("0.01")


def test_refit_writes_basket_returns(db_conn):
    seed_themes(db_conn)
    seed_bars_for(db_conn, SYMBOLS, sessions=121)
    refit(db_conn, D, now_utc=NOW, model_version=MV)
    n = db_conn.execute(text(
        "SELECT count(*) FROM basket_returns WHERE model_version = :mv"
    ), {"mv": MV}).scalar_one()
    assert n > 0


def test_score_is_idempotent(db_conn):
    seed_themes(db_conn)
    seed_bars_for(db_conn, SYMBOLS, sessions=121)
    refit(db_conn, D, now_utc=NOW, model_version=MV)
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=False)
    first = db_conn.execute(text(
        "SELECT count(*) FROM attribution WHERE trade_date = :d AND model_version = :mv"
    ), {"d": D, "mv": MV}).scalar_one()
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=False)
    second = db_conn.execute(text(
        "SELECT count(*) FROM attribution WHERE trade_date = :d AND model_version = :mv"
    ), {"d": D, "mv": MV}).scalar_one()
    assert first == second and first > 0
