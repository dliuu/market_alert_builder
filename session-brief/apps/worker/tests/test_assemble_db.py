"""M4 against a real database (skipped without DATABASE_URL): assemble computes,
builds the object, and upserts it into ``briefs`` — idempotently on
(user_id, session_date, kind)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import SCHEMA_VERSION, assemble_and_store

# A throwaway user, distinct from the real dev tenant.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fd"

_PREV = date(2026, 8, 10)
_SESSION = date(2026, 8, 11)
_SECTOR = "ZZASSEM"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-assemble@example.invalid')"),
        {"u": _TEST_USER_ID},
    )


def _seed_bar(conn: Connection, symbol: str, session_date: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": session_date, "c": Decimal(close)},
    )


def _seed_lot(conn: Connection, symbol: str, shares: str, cost_cents: int) -> None:
    sector_id = conn.execute(
        text(
            "INSERT INTO sectors (user_id, name) VALUES (:u, :n) "
            "ON CONFLICT (user_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"u": _TEST_USER_ID, "n": _SECTOR},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": _TEST_USER_ID, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, :sh, :cb, :o)"
        ),
        {"u": _TEST_USER_ID, "h": holding_id, "sh": Decimal(shares), "cb": cost_cents, "o": _PREV},
    )


def _seed_book(conn: Connection) -> None:
    # Junk symbols only: bars_daily is shared (no user_id), so seeding a real
    # benchmark like SPY would collide with backfilled data. vs_spy is exercised
    # by the pure test; here we care about persistence and idempotency.
    _seed_user(conn)
    for symbol, prev_c, c in (("ZZA", "100", "110"), ("ZZB", "50", "48")):
        _seed_bar(conn, symbol, _PREV, prev_c)
        _seed_bar(conn, symbol, _SESSION, c)
    _seed_lot(conn, "ZZA", "10", 9000)  # cost $90
    _seed_lot(conn, "ZZB", "20", 4000)  # cost $40


def _brief_rows(conn: Connection) -> list[dict]:
    return (
        conn.execute(
            text(
                "SELECT kind, schema_version, body FROM briefs "
                "WHERE user_id = :u AND session_date = :d AND kind = 'close'"
            ),
            {"u": _TEST_USER_ID, "d": _SESSION},
        )
        .mappings()
        .all()
    )


def test_assemble_and_store_persists_the_object(db_conn: Connection) -> None:
    _seed_book(db_conn)

    obj = assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")

    rows = _brief_rows(db_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == SCHEMA_VERSION
    # The stored jsonb is exactly the object we returned.
    assert row["body"] == obj.model_dump(mode="json")
    # Close prices made it into the attribution rows.
    body = row["body"]
    rows_by_symbol = {r["symbol"]: r for r in body["sections"][0]["rows"]}
    assert rows_by_symbol["ZZA"]["close"] == 110.0
    assert body["book"]["day_bps"] == 300


def test_reassembly_upserts_not_duplicates(db_conn: Connection) -> None:
    _seed_book(db_conn)

    assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")
    assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")

    assert len(_brief_rows(db_conn)) == 1
