"""The open brief end to end against a real database (skipped without
DATABASE_URL). Rolled back per test.

The DoD that only a DB run can show: it always sends (no skip gate, even on a
day with nothing in it), it never touches the P&L path, and §5 reads real bars.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble_open import assemble_open_and_store, read_open_inputs
from worker.events_seed import seed_events

_USER = "00000000-0000-0000-0000-0000000000fb"
_SESSION = date(2098, 3, 12)
_PRIOR = date(2098, 3, 11)
_GENERATED_AT = datetime(2098, 3, 12, 12, 15, tzinfo=UTC)
_BENCH = "ZBENCH"


def _seed_book(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-open-db@example.invalid')"),
        {"u": _USER},
    )
    sector_id = conn.execute(
        text(
            "INSERT INTO sectors (user_id, name, benchmark_symbol) "
            "VALUES (:u, 'ZZSECTOR', :b) RETURNING id"
        ),
        {"u": _USER, "b": _BENCH},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol, status) "
            "VALUES (:u, :s, 'ZHELD', 'owned') RETURNING id"
        ),
        {"u": _USER, "s": sector_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol, status) "
            "VALUES (:u, :s, 'ZWATCH', 'watching')"
        ),
        {"u": _USER, "s": sector_id},
    )
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 1000, :d)"
        ),
        {"u": _USER, "h": holding_id, "d": date(2098, 1, 2)},
    )


def _seed_bars(conn: Connection, symbol: str, start: Decimal, step: Decimal) -> None:
    """Ten consecutive daily bars ending at the prior session."""
    price = start
    for i in range(10, 0, -1):
        session = _PRIOR - timedelta(days=i - 1)
        conn.execute(
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c) ON CONFLICT DO NOTHING"
            ),
            {"s": symbol, "d": session, "c": price},
        )
        price += step


def test_open_brief_stores_a_book_less_body(db_conn: Connection) -> None:
    _seed_book(db_conn)
    _seed_bars(db_conn, "ZHELD", Decimal("10"), Decimal("0.5"))
    _seed_bars(db_conn, _BENCH, Decimal("100"), Decimal("1"))
    _seed_bars(db_conn, "SPY", Decimal("500"), Decimal("1"))
    seed_events(db_conn, session_date=_SESSION, symbols=["ZHELD", "ZWATCH"])

    obj = assemble_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    assert obj.kind.value == "open"
    assert obj.book is None

    stored = db_conn.execute(
        text("SELECT body FROM briefs WHERE user_id = :u AND kind = 'open'"), {"u": _USER}
    ).scalar_one()
    assert stored["book"] is None
    assert stored["schema_version"] == 3


def test_sector_setup_reads_real_bars(db_conn: Connection) -> None:
    _seed_book(db_conn)
    _seed_bars(db_conn, _BENCH, Decimal("100"), Decimal("1"))
    _seed_bars(db_conn, "SPY", Decimal("500"), Decimal("1"))

    obj = assemble_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    setup = next(s for s in obj.sections if s.id.value == "sector_setup")
    row = setup.rows[0]
    assert row.benchmark_symbol == _BENCH
    # Closes 100..109; five sessions back from 109 is 104 → 109/104 - 1.
    assert row.ret_5d is not None
    assert abs(row.ret_5d - (109 / 104 - 1)) < 1e-9
    assert row.vs_spy_5d is not None  # SPY has bars too


def test_always_sends_on_a_day_with_nothing_in_it(db_conn: Connection) -> None:
    """DoD 2. No events, no bars, no flags — still a stored, valid brief."""
    _seed_book(db_conn)

    obj = assemble_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    assert obj is not None
    calendar = next(s for s in obj.sections if s.id.value == "calendar")
    assert calendar.tier.value == "suppressed"
    assert calendar.note is not None

    count = db_conn.execute(
        text("SELECT count(*) FROM briefs WHERE user_id = :u AND kind = 'open'"), {"u": _USER}
    ).scalar_one()
    assert count == 1


def test_missing_bars_do_not_stop_the_open_brief(db_conn: Connection) -> None:
    """The close path raises when a held name has no bar for the session. The
    open brief must not — it always sends, so it can never depend on that."""
    _seed_book(db_conn)  # holdings and lots, but no bars_daily rows at all

    obj = assemble_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    setup = next(s for s in obj.sections if s.id.value == "sector_setup")
    assert setup.rows[0].ret_5d is None  # degraded, not raised


def test_is_idempotent_on_user_session_kind(db_conn: Connection) -> None:
    _seed_book(db_conn)
    for _ in range(2):
        assemble_open_and_store(
            db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
        )

    count = db_conn.execute(
        text("SELECT count(*) FROM briefs WHERE user_id = :u AND kind = 'open'"), {"u": _USER}
    ).scalar_one()
    assert count == 1


def test_book_symbols_includes_sector_benchmarks(db_conn: Connection) -> None:
    """§5 reads bars for each sector's benchmark, so ingest has to fetch them.
    They were settable in the book since M1 but never in the refresh set — so
    the trailing-5d line had no bars and rendered a dash forever."""
    from worker.scheduler import book_symbols

    _seed_book(db_conn)

    symbols = book_symbols(db_conn, _USER)

    assert _BENCH in symbols  # the sector benchmark, which is in no holding
    assert "ZHELD" in symbols
    assert "SPY" in symbols  # the market benchmark is always included


def test_calendar_tags_come_from_holding_status(db_conn: Connection) -> None:
    _seed_book(db_conn)
    seed_events(db_conn, session_date=_SESSION, symbols=["ZHELD", "ZWATCH"])

    events, _sectors, holdings = read_open_inputs(db_conn, _USER, _SESSION, _PRIOR)

    assert holdings == {"ZHELD": "owned", "ZWATCH": "watching"}
    assert any(e.symbol is None for e in events)  # macro rows survive the read
