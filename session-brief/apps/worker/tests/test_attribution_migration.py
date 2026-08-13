from sqlalchemy import text


def test_attribution_tables_exist_and_are_shared(db_conn):
    for table in ("themes", "theme_members", "basket_returns",
                  "attribution_fits", "attribution"):
        db_conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))  # no error = exists

    # Shared: attribution tables must NOT carry user_id.
    for table in ("attribution", "attribution_fits", "basket_returns"):
        cols = db_conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ), {"t": table}).scalars().all()
        assert "user_id" not in cols
