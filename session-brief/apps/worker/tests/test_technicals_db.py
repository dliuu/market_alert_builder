"""M19 against a real database (skipped without DATABASE_URL): technicals read
the adjusted window, store the numeric metrics, and refuse to guess where the
adjusted series is missing."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.technicals import compute_and_store_technicals

# A dedicated throwaway user, distinct from the real dev tenant — these tests
# must never read or interact with someone's actual book.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fe"

_SESSION = date(2026, 8, 11)
_CYCLE = ["100", "105", "110", "105", "100", "95", "90", "95"]


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-technicals@example.invalid')"),
        {"u": _TEST_USER_ID},
    )


def _seed_sawtooth(
    conn: Connection, symbol: str, n: int, *, adjusted: bool = True, end: date = _SESSION
) -> None:
    """``n`` sessions ending on ``end``, turning at 110 and 90."""
    for i in range(n):
        session = end - timedelta(days=n - 1 - i)
        price = Decimal(_CYCLE[i % 8])
        conn.execute(
            text(
                "INSERT INTO bars_daily "
                "(symbol, session_date, o, h, l, c, v, adj_c, adj_o, adj_h, adj_l, adj_v) "
                "VALUES (:s, :d, :p, :p, :p, :p, 1000, :p, :a, :a, :a, :av)"
            ),
            {
                "s": symbol,
                "d": session,
                "p": price,
                "a": price if adjusted else None,
                "av": 1000 if adjusted else None,
            },
        )


def _stored(conn: Connection, symbol: str, metric: str) -> Decimal | None:
    return conn.execute(
        text(
            "SELECT value FROM metrics WHERE user_id = :u AND symbol = :s "
            "AND session_date = :d AND metric = :m"
        ),
        {"u": _TEST_USER_ID, "s": symbol, "d": _SESSION, "m": metric},
    ).scalar_one_or_none()


def test_computes_and_stores_the_numeric_metrics(db_conn: Connection) -> None:
    _seed_user(db_conn)
    _seed_sawtooth(db_conn, "ZZT", 81)

    out = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZT"], _SESSION, rvol={"ZZT": None}
    )

    assert out["ZZT"].support is not None
    assert out["ZZT"].support.price == Fraction(90)
    assert out["ZZT"].resistance is not None
    assert out["ZZT"].resistance.price == Fraction(110)
    assert _stored(db_conn, "ZZT", "support") == Decimal("90.0000000000")
    assert _stored(db_conn, "ZZT", "resistance") == Decimal("110.0000000000")
    assert _stored(db_conn, "ZZT", "ma_20") is not None
    assert _stored(db_conn, "ZZT", "atr_14") == Decimal("5.0000000000")


def test_a_symbol_without_the_adjusted_series_is_skipped_not_guessed(
    db_conn: Connection,
) -> None:
    # Rows predating the M19 replay have a null adj_h. Falling back to the raw
    # high would produce a confident, split-contaminated level.
    _seed_user(db_conn)
    _seed_sawtooth(db_conn, "ZZU", 81, adjusted=False)

    out = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZU"], _SESSION, rvol={"ZZU": None}
    )

    assert out == {}
    assert _stored(db_conn, "ZZU", "support") is None


def test_a_symbol_without_a_bar_on_the_session_is_skipped(db_conn: Connection) -> None:
    _seed_user(db_conn)
    _seed_sawtooth(db_conn, "ZZV", 81)
    db_conn.execute(
        text("DELETE FROM bars_daily WHERE symbol = 'ZZV' AND session_date = :d"),
        {"d": _SESSION},
    )

    out = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZV"], _SESSION, rvol={"ZZV": None}
    )

    assert out == {}


def test_levels_land_on_todays_tape_after_a_split(db_conn: Connection) -> None:
    # Adjusted prices are half the raw ones, as after a 2:1 split. The engine
    # runs on the adjusted series but the level it reports must be in the
    # numbers the reader sees on their screen.
    _seed_user(db_conn)
    for i in range(81):
        session = _SESSION - timedelta(days=80 - i)
        adj = Decimal(_CYCLE[i % 8])
        db_conn.execute(
            text(
                "INSERT INTO bars_daily "
                "(symbol, session_date, o, h, l, c, v, adj_c, adj_o, adj_h, adj_l, adj_v) "
                "VALUES (:s, :d, :r, :r, :r, :r, 1000, :a, :a, :a, :a, 1000)"
            ),
            {"s": "ZZW", "d": session, "r": adj * 2, "a": adj},
        )

    out = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZW"], _SESSION, rvol={"ZZW": None}
    )

    assert out["ZZW"].resistance is not None
    assert out["ZZW"].resistance.price == Fraction(220)  # 110 adjusted, 220 on the tape
    assert out["ZZW"].support is not None
    assert out["ZZW"].support.price == Fraction(180)


def test_a_breakout_needs_the_rvol_passed_in(db_conn: Connection) -> None:
    _seed_user(db_conn)
    # 80 sessions ending the day before, at 95 on the way down.
    _seed_sawtooth(db_conn, "ZZX", 80, end=_SESSION - timedelta(days=1))
    db_conn.execute(
        text(
            "INSERT INTO bars_daily "
            "(symbol, session_date, o, h, l, c, v, adj_c, adj_o, adj_h, adj_l, adj_v) "
            "VALUES ('ZZX', :d, 85, 85, 85, 85, 5000, 85, 85, 85, 85, 5000)"
        ),
        {"d": _SESSION},
    )

    quiet = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZX"], _SESSION, rvol={"ZZX": Fraction(1)}
    )
    loud = compute_and_store_technicals(
        db_conn, _TEST_USER_ID, ["ZZX"], _SESSION, rvol={"ZZX": Fraction(3)}
    )

    assert quiet["ZZX"].breakout is None
    assert loud["ZZX"].breakout == "down"
