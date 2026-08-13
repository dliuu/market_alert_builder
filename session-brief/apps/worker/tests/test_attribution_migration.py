from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def test_attribution_tables_exist_and_are_shared(db_conn: Connection) -> None:
    for table in ("themes", "theme_members", "basket_returns",
                  "attribution_fits", "attribution"):
        db_conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))  # no error = exists

    # Shared: attribution tables must NOT carry user_id.
    for table in ("attribution", "attribution_fits", "basket_returns"):
        cols = db_conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ), {"t": table}).scalars().all()
        assert "user_id" not in cols


def test_m12_econometrics_schema_present_and_shared(db_conn: Connection) -> None:
    for table in ("basket_loo_returns", "index_events",
                  "attribution_signals", "theme_dispersion"):
        db_conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))  # exists

    attr_cols = db_conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='attribution'"
    )).scalars().all()
    assert "resid_z" in attr_cols

    basket_cols = db_conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='basket_returns'"
    )).scalars().all()
    assert "weights" in basket_cols

    # Shared reference data: no user_id on the new tables.
    for table in (
        "basket_loo_returns", "index_events", "attribution_signals", "theme_dispersion",
    ):
        cols = db_conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ), {"t": table}).scalars().all()
        assert "user_id" not in cols
