"""`surface_flags` has to work for the open brief, which has no `ComputeResult`.

§6 (exposure check) is the flags' documented home (docs/05), but the open brief
carries no P&L and must never go through the compute path to manufacture one —
that path raises on any missing bar, and the open brief always sends. So the
mechanism takes the two things it actually needs (the held symbols and their
weights) instead of the whole P&L result.

DB-backed (skipped without DATABASE_URL); rolled back per test.
"""

from __future__ import annotations

from datetime import date, timedelta
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


# --- The mechanism moved: the close brief no longer surfaces flags ---------


def _seed_a_sending_close_session(db_conn: Connection) -> date:
    """A one-name book that moves +10%, so the close brief is a real send and
    not a skipped quiet session. Returns the session date."""
    prev, session = date(2099, 9, 1), date(2099, 9, 2)
    holding_id = db_conn.execute(
        text("SELECT id FROM holdings WHERE user_id = :u AND symbol = 'ZOPN'"), {"u": _USER}
    ).scalar_one()
    db_conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 10000, :d)"
        ),
        {"u": _USER, "h": holding_id, "d": prev - timedelta(days=1)},
    )
    for d, c in [(prev, "100"), (session, "110")]:
        db_conn.execute(
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES ('ZOPN', :d, :c, :c, :c, :c, 1000, :c) ON CONFLICT DO NOTHING"
            ),
            {"d": d, "c": c},
        )
    return session


def test_close_brief_surfaces_no_flags(db_conn: Connection) -> None:
    """§6 is the open brief's section (docs/05). `flags.last_seen` is a single
    clock, so if both briefs surfaced flags the earlier fire would spend the
    week's budget and the later one would silently render nothing. The close
    brief therefore stops surfacing them entirely (M14)."""
    from worker.assemble import assemble_and_store

    _seed_book(db_conn)
    session = _seed_a_sending_close_session(db_conn)

    obj = assemble_and_store(db_conn, _USER, session, "close")

    assert obj is not None  # it sent — this is not a skipped quiet session
    assert obj.flags == []


def test_close_brief_spends_no_flag_budget(db_conn: Connection) -> None:
    """Following from the above: a close brief leaves `last_seen` untouched, so
    the morning's §6 gets the full weekly budget."""
    from worker.assemble import assemble_and_store

    _seed_book(db_conn)
    session = _seed_a_sending_close_session(db_conn)

    assemble_and_store(db_conn, _USER, session, "close")

    recorded = db_conn.execute(
        text("SELECT count(*) FROM flags WHERE user_id = :u"), {"u": _USER}
    ).scalar_one()
    assert recorded == 0
