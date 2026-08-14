"""M14 §4 "On the clock today" needs `events` to hold two things the M7 table
rejects: an `ex_div` type, and a macro release with no ticker at all. Plus a
seeder, because nothing has ever written to the table (D18 seeded it by hand).

DB-backed (skipped without DATABASE_URL); each test runs in a rolled-back
transaction (conftest). Far-future dates so nothing collides with real data.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.events_seed import seed_events

_SESSION = date(2099, 4, 15)


def _rows(
    conn: Connection, occurs_from: date, *, label_like: str = "%"
) -> list[dict[str, object]]:
    """Rows from `occurs_from` on. `label_like` scopes a query to just what a
    test inserted — the table is shared reference data with no user_id, so a
    test must never assume the date range is otherwise empty."""
    return [
        dict(r)
        for r in conn.execute(
            text(
                "SELECT symbol, event_type, occurs_at, label FROM events "
                "WHERE occurs_at >= :d AND label LIKE :label "
                "ORDER BY occurs_at, event_type, symbol NULLS FIRST"
            ),
            {"d": occurs_from, "label": label_like},
        ).mappings()
    ]


# --- What the M7 table shape rejects --------------------------------------


def test_events_accepts_an_ex_div_row(db_conn: Connection) -> None:
    db_conn.execute(
        text(
            "INSERT INTO events (symbol, event_type, occurs_at, label) "
            "VALUES ('ZTEST', 'ex_div', :d, 'ZTEST goes ex-dividend')"
        ),
        {"d": _SESSION},
    )
    rows = _rows(db_conn, _SESSION, label_like="ZTEST goes%")
    assert [r["event_type"] for r in rows] == ["ex_div"]


def test_events_accepts_a_macro_row_without_a_symbol(db_conn: Connection) -> None:
    """A CPI print names no security — `symbol` was NOT NULL."""
    db_conn.execute(
        text(
            "INSERT INTO events (symbol, event_type, occurs_at, label) "
            "VALUES (NULL, 'macro', :d, 'CPI (Mar)')"
        ),
        {"d": _SESSION},
    )
    rows = _rows(db_conn, _SESSION, label_like="CPI (Mar)")
    assert rows[0]["symbol"] is None
    assert rows[0]["label"] == "CPI (Mar)"


# --- The seeder -----------------------------------------------------------


def test_seed_events_writes_macro_and_per_symbol_events(db_conn: Connection) -> None:
    seed_events(db_conn, session_date=_SESSION, symbols=["ZAAA", "ZBBB"])

    rows = _rows(db_conn, _SESSION)
    assert rows, "seeder wrote nothing"

    types = {r["event_type"] for r in rows}
    assert "macro" in types  # the calendar is not just company events
    assert "earnings" in types

    # Every row is labelled — §4 renders the label, not the raw type.
    assert all(r["label"] for r in rows)

    # Macro rows carry no ticker; company rows always do.
    for r in rows:
        if r["event_type"] == "macro":
            assert r["symbol"] is None
        else:
            assert r["symbol"] in {"ZAAA", "ZBBB"}


def test_seed_events_is_idempotent(db_conn: Connection) -> None:
    """Re-seeding must not duplicate the calendar — the M7 fundamentals/events
    pattern is re-runnable."""
    seed_events(db_conn, session_date=_SESSION, symbols=["ZAAA", "ZBBB"])
    first = _rows(db_conn, _SESSION)

    seed_events(db_conn, session_date=_SESSION, symbols=["ZAAA", "ZBBB"])
    second = _rows(db_conn, _SESSION)

    assert first == second


def test_seed_events_lands_inside_the_calendar_window(db_conn: Connection) -> None:
    """§4 reads a forward window off the session date; seeded events have to be
    in it or the section is empty on the very day it's demonstrated."""
    seed_events(db_conn, session_date=_SESSION, symbols=["ZAAA"])

    rows = _rows(db_conn, _SESSION)
    on_the_day = [r for r in rows if r["occurs_at"] == _SESSION]
    assert on_the_day, "nothing seeded on the session date itself"
