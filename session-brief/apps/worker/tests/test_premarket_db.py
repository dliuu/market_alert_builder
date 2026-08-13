"""The `quotes` table and the pre-market ingest path (M15), against a real
database (skipped without DATABASE_URL). Rolled back per test."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

_SESSION = date(2098, 4, 7)
_CAPTURED = datetime(2098, 4, 7, 12, 12, tzinfo=UTC)


def test_quotes_upsert_is_idempotent_per_session(db_conn: Connection) -> None:
    """One capture per symbol per session — re-running the pre-open seed
    replaces rather than duplicating (the events_seed precedent)."""
    upsert = text("""
        INSERT INTO quotes (symbol, session_date, captured_at, last, prev_close,
                            extended_last, extended_v)
        VALUES (:s, :d, :t, :last, :prev, :ext, :extv)
        ON CONFLICT (symbol, session_date) DO UPDATE
            SET captured_at = EXCLUDED.captured_at,
                last = EXCLUDED.last,
                prev_close = EXCLUDED.prev_close,
                extended_last = EXCLUDED.extended_last,
                extended_v = EXCLUDED.extended_v
    """)
    row = {
        "s": "ZQUOTE", "d": _SESSION, "t": _CAPTURED,
        "last": Decimal("10.00"), "prev": Decimal("9.50"),
        "ext": Decimal("10.25"), "extv": 12345,
    }
    db_conn.execute(upsert, row)
    db_conn.execute(upsert, {**row, "ext": Decimal("10.75")})

    stored = db_conn.execute(
        text("SELECT extended_last, extended_v FROM quotes "
             "WHERE symbol = :s AND session_date = :d"),
        {"s": "ZQUOTE", "d": _SESSION},
    ).all()
    assert len(stored) == 1
    assert Decimal(str(stored[0][0])) == Decimal("10.75")
    assert stored[0][1] == 12345


def test_quotes_carries_no_user_id(db_conn: Connection) -> None:
    """Market data is shared across the tenant base, keyed by symbol (D18/D21)."""
    columns = {
        r[0]
        for r in db_conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name = 'quotes'")
        ).all()
    }
    assert "user_id" not in columns
    assert {"symbol", "session_date", "captured_at", "last", "prev_close",
            "extended_last", "extended_v"} <= columns
