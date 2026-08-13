from datetime import UTC, date, datetime

from sqlalchemy import text

from tests.helpers_attribution import seed_bars_for
from worker.attribution import refit, score
from worker.constants import ATTRIBUTION_MODEL_VERSION as MV
from worker.reconcile import needs_revision, reconcile
from worker.themes_seed import seed_themes

SYMBOLS = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "SPY"]
D = date(2020, 6, 30)
NOW = datetime(2020, 6, 30, 22, 0, tzinfo=UTC)


def test_needs_revision_threshold():
    assert needs_revision(0.05, 0.02, tol=0.001) is True
    assert needs_revision(0.0201, 0.02, tol=0.001) is False


def test_pm_synth_then_am_reconcile_flips_revised_and_converges(db_conn):
    seed_themes(db_conn)
    seed_bars_for(db_conn, SYMBOLS, sessions=121)
    refit(db_conn, D, now_utc=NOW, model_version=MV)

    # PM run: score marks rows synthetic/provisional. Then simulate a divergent
    # synthetic bar for NVDA by corrupting its stored return (a wrong PM minute).
    score(db_conn, D, now_utc=NOW, model_version=MV, synthetic=True)
    db_conn.execute(text("""
        UPDATE attribution SET total_bps = 99999
        WHERE trade_date = :d AND symbol = 'NVDA' AND model_version = :mv
    """), {"d": D, "mv": MV})

    # Capture the official-only decomposition for NVDA (what AM should converge to).
    result = reconcile(db_conn, D, now_utc=NOW, model_version=MV)
    assert "NVDA" in result.revised

    row = db_conn.execute(text("""
        SELECT revised, provisional, synthetic, total_bps FROM attribution
        WHERE trade_date = :d AND symbol = 'NVDA' AND model_version = :mv
    """), {"d": D, "mv": MV}).mappings().one()
    assert row["revised"] is True
    assert row["provisional"] is False
    assert row["synthetic"] is False
    assert abs(float(row["total_bps"])) < 90000  # corrupted 99999 was corrected

    # Re-runnable: a second reconcile finds no synthetic rows -> no-op.
    again = reconcile(db_conn, D, now_utc=NOW, model_version=MV)
    assert again.revised == [] and again.unchanged == []
