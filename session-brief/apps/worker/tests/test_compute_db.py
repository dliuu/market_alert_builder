"""M3 against a real database (skipped without DATABASE_URL): compute reads the
book + bars, stores metrics, the stored contributions sum to the book return,
and a recompute is deterministic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.compute import compute_and_store

# A dedicated throwaway user, distinct from the real dev tenant — these tests
# must never read or interact with someone's actual book.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fe"

_PREV = date(2026, 8, 10)
_SESSION = date(2026, 8, 11)
_SECTOR = "ZZSECT"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-compute@example.invalid')"),
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


def _stored(conn: Connection, symbol: str, metric: str) -> Decimal:
    value = conn.execute(
        text(
            "SELECT value FROM metrics WHERE user_id = :u AND symbol = :s "
            "AND session_date = :d AND metric = :m"
        ),
        {"u": _TEST_USER_ID, "s": symbol, "d": _SESSION, "m": metric},
    ).scalar_one()
    assert isinstance(value, Decimal)
    return value


def test_compute_stores_metrics_and_contributions_sum_to_day_bps(db_conn: Connection) -> None:
    _seed_user(db_conn)
    # Book: ZZA held (prev 100 → 110), ZZB held (prev 50 → 48).
    for symbol, prev_c, c in (("ZZA", "100", "110"), ("ZZB", "50", "48")):
        _seed_bar(db_conn, symbol, _PREV, prev_c)
        _seed_bar(db_conn, symbol, _SESSION, c)
    _seed_lot(db_conn, "ZZA", "10", 9000)  # cost $90
    _seed_lot(db_conn, "ZZB", "20", 4000)  # cost $40

    result = compute_and_store(db_conn, _TEST_USER_ID, _SESSION)

    # book: prior 200000c, day +6000c → 300 bps; contributions +500 / -200.
    assert result.book.day_pnl_cents == 6000
    assert result.book.day_bps == 300

    assert _stored(db_conn, "ZZA", "day_pnl_cents") == Decimal("10000")
    assert _stored(db_conn, "ZZB", "day_pnl_cents") == Decimal("-4000")
    assert _stored(db_conn, "ZZA", "contribution_bps") == Decimal("500").quantize(
        Decimal("0.0000000001")
    )

    stored_sum = _stored(db_conn, "ZZA", "contribution_bps") + _stored(
        db_conn, "ZZB", "contribution_bps"
    )
    assert stored_sum == Decimal("300").quantize(Decimal("0.0000000001"))


def test_recompute_is_idempotent(db_conn: Connection) -> None:
    _seed_user(db_conn)
    _seed_bar(db_conn, "ZZA", _PREV, "100")
    _seed_bar(db_conn, "ZZA", _SESSION, "110")
    _seed_lot(db_conn, "ZZA", "10", 9000)

    first = compute_and_store(db_conn, _TEST_USER_ID, _SESSION)
    before = _stored(db_conn, "ZZA", "day_pnl_cents")
    second = compute_and_store(db_conn, _TEST_USER_ID, _SESSION)
    after = _stored(db_conn, "ZZA", "day_pnl_cents")

    assert first == second
    assert before == after


def test_missing_bar_raises_with_symbol(db_conn: Connection) -> None:
    # Lot exists but no bar for the session → a clear data-gap error.
    _seed_user(db_conn)
    _seed_lot(db_conn, "ZZX", "5", 1000)
    try:
        compute_and_store(db_conn, _TEST_USER_ID, _SESSION)
    except ValueError as exc:
        assert "ZZX" in str(exc)
    else:
        raise AssertionError("expected ValueError naming the symbol with no bars")
