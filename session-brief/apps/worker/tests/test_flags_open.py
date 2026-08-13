"""`surface_flags` has to work for the open brief, which has no `ComputeResult`.

§6 (exposure check) is the flags' documented home (docs/05), but the open brief
carries no P&L and must never go through the compute path to manufacture one —
that path raises on any missing bar, and the open brief always sends. So the
mechanism takes the two things it actually needs (the held symbols and their
weights) instead of the whole P&L result.

DB-backed (skipped without DATABASE_URL); rolled back per test.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.flags import surface_flags

_USER = "00000000-0000-0000-0000-0000000000fd"
_SESSION = date(2099, 6, 10)


def _seed_book(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-open-flags@example.invalid')"),
        {"u": _USER},
    )
    sector_id = conn.execute(
        text("INSERT INTO sectors (user_id, name) VALUES (:u, 'ZZOPEN') RETURNING id"),
        {"u": _USER},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, 'ZOPN')"
        ),
        {"u": _USER, "s": sector_id},
    )


def test_surfaces_concentration_from_weights_without_a_compute_result(
    db_conn: Connection,
) -> None:
    """The open brief's §6 path: weights in, flags out, no P&L anywhere."""
    _seed_book(db_conn)

    surfaced = surface_flags(
        db_conn,
        _USER,
        _SESSION,
        symbols=["ZOPN"],
        name_weights={"ZOPN": Fraction(9, 10)},  # 90% of book — over the 20% cap
    )

    # 90% of the book is also >50% of its sector, so both concentration
    # thresholds fire — names first, then sectors (deterministic order).
    concentration = [c for c in surfaced if c.type == "concentration"]
    assert [c.text_key for c in concentration] == [
        "single_name_concentration",
        "sector_concentration",
    ]
    assert concentration[0].symbol == "ZOPN"
    assert concentration[1].symbol is None and concentration[1].sector_id is not None


def test_a_symbol_with_no_weight_still_gets_position_risk(db_conn: Connection) -> None:
    """`symbols` and `name_weights` are separate on purpose: a position can have
    no weight (an empty book has no denominator) and must still be screened for
    position risk. Folding the two together would silently drop it."""
    _seed_book(db_conn)
    db_conn.execute(
        text(
            "INSERT INTO events (symbol, event_type, occurs_at, label) "
            "VALUES ('ZOPN', 'lockup', :d, 'ZOPN lockup expiry')"
        ),
        {"d": _SESSION},
    )

    surfaced = surface_flags(
        db_conn, _USER, _SESSION, symbols=["ZOPN"], name_weights={}
    )

    assert [c.text_key for c in surfaced] == ["supply_event_soon"]
