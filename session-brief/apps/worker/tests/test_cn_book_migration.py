"""Migration 0015: sectors.market column for multi-book support."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError


def test_sectors_market_column_exists(db_conn: Connection) -> None:
    """Verify the market column exists on sectors table."""
    cols = db_conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'sectors'"
    )).scalars().all()
    assert "market" in cols


def test_sectors_market_default_value(db_conn: Connection) -> None:
    """Insert a sector without specifying market; should default to 'US'."""
    user_id = db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (gen_random_uuid(), "
             "'test-market@example.invalid') RETURNING id")
    ).scalar_one()

    sector_id = db_conn.execute(
        text(
            "INSERT INTO sectors (id, user_id, name) "
            "VALUES (gen_random_uuid(), :u, 'Tech') RETURNING id"
        ),
        {"u": user_id},
    ).scalar_one()

    market = db_conn.execute(
        text("SELECT market FROM sectors WHERE id = :id"),
        {"id": sector_id},
    ).scalar_one()

    assert market == "US"


def test_sectors_market_constraint_rejects_invalid_value(db_conn: Connection) -> None:
    """Inserting market='JP' should raise an integrity error."""
    user_id = db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (gen_random_uuid(), "
             "'test-market-constraint@example.invalid') RETURNING id")
    ).scalar_one()

    with pytest.raises(IntegrityError):
        db_conn.execute(
            text(
                "INSERT INTO sectors (id, user_id, name, market) "
                "VALUES (gen_random_uuid(), :u, 'Tech', 'JP')"
            ),
            {"u": user_id},
        )
