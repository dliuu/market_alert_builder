from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.themes_seed import seed_themes


def test_seed_is_idempotent_and_pit(db_conn: Connection) -> None:
    seed_themes(db_conn)
    first = db_conn.execute(text("SELECT count(*) FROM theme_members")).scalar_one()
    seed_themes(db_conn)  # re-run
    second = db_conn.execute(text("SELECT count(*) FROM theme_members")).scalar_one()
    assert first == second and first > 0

    # Exactly one is_primary per symbol at any effective date.
    dupes = db_conn.execute(text("""
        SELECT symbol, count(*) FROM theme_members
        WHERE is_primary AND effective_to IS NULL
        GROUP BY symbol HAVING count(*) > 1
    """)).all()
    assert dupes == []
