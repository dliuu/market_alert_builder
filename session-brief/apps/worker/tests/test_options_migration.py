"""0020_options: the four tables exist with the primary keys the spec names."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

EXPECTED_PKS = {
    "option_contracts": ["contract"],
    "option_snapshot": ["contract", "session_date"],
    "option_strip": ["symbol", "session_date", "tenor", "strike_rule"],
    "index_daily": ["symbol", "session_date"],
}


@pytest.mark.parametrize(("table", "columns"), EXPECTED_PKS.items())
def test_primary_key(db_conn: Connection, table: str, columns: list[str]) -> None:
    rows = db_conn.execute(
        text("""
            SELECT kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema = tc.table_schema
             WHERE tc.table_name = :t AND tc.constraint_type = 'PRIMARY KEY'
             ORDER BY kcu.ordinal_position
        """),
        {"t": table},
    ).all()
    assert [r[0] for r in rows] == columns


@pytest.mark.parametrize("table", EXPECTED_PKS)
def test_public_read_policy(db_conn: Connection, table: str) -> None:
    policy = db_conn.execute(
        text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = :p"),
        {"t": table, "p": f"{table}_read"},
    ).scalar()
    assert policy == 1
