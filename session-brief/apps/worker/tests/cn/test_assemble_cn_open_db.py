"""``assemble_cn_open_and_store`` end to end against a real database (skipped
without DATABASE_URL), plus the cross-book calendar filter (change 2, CN-M2):
the US and CN books' §4 must never bleed into each other.

Symbols are ZZ-prefixed synthetic tickers, never real A-share or US symbols,
per the convention `tests/cn/test_assemble_cn_db.py` established — the shared
dev DB already holds committed rows for real tickers (including the CN
benchmark, 000300.SS), so a DB test must own symbols no other test or run
would ever write. ``CN_BENCHMARK`` is deliberately never seeded here, same as
that file: a missing benchmark degrades ``vs_spy_5d`` to ``None`` rather than
raising."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble_open import assemble_open_and_store
from worker.events_seed import seed_events
from worker_cn.assemble import assemble_cn_open_and_store

_USER = "00000000-0000-0000-0000-0000000000c9"
_XBLEED_USER = "00000000-0000-0000-0000-0000000000ca"

_SESSION = date(2098, 5, 12)
_PRIOR = date(2098, 5, 11)
_GENERATED_AT = datetime(2098, 5, 12, 1, 10, tzinfo=UTC)


def _seed_user(conn: Connection, user_id: str, email: str) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, :e)"), {"u": user_id, "e": email}
    )


def _seed_cn_sector(conn: Connection, user_id: str, name: str, benchmark: str | None) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO sectors (user_id, name, benchmark_symbol, market) "
                "VALUES (:u, :n, :b, 'CN') RETURNING id"
            ),
            {"u": user_id, "n": name, "b": benchmark},
        ).scalar_one()
    )


def _seed_holding(
    conn: Connection, user_id: str, sector_id: str, symbol: str, status: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol, status) "
            "VALUES (:u, :s, :sym, :st)"
        ),
        {"u": user_id, "s": sector_id, "sym": symbol, "st": status},
    )


def _seed_bars(conn: Connection, symbol: str, start: Decimal, step: Decimal, end: date) -> None:
    """Ten consecutive daily bars ending at ``end``."""
    price = start
    for i in range(10, 0, -1):
        session = end - timedelta(days=i - 1)
        conn.execute(
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c) ON CONFLICT DO NOTHING"
            ),
            {"s": symbol, "d": session, "c": price},
        )
        price += step


def _brief_row_count(conn: Connection, user_id: str, session_date: date, kind: str) -> int:
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM briefs "
                "WHERE user_id = :u AND session_date = :d AND kind = :k"
            ),
            {"u": user_id, "d": session_date, "k": kind},
        ).scalar_one()
    )


def test_assemble_cn_open_and_store_writes_a_book_less_row(db_conn: Connection) -> None:
    _seed_user(db_conn, _USER, "test-cn-open@example.invalid")
    sector = _seed_cn_sector(db_conn, _USER, "ZZ-CN-O-1", None)
    _seed_holding(db_conn, _USER, sector, "ZZCNO9.SS", "owned")

    obj = assemble_cn_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    assert obj.kind.value == "open_cn"
    assert obj.book is None
    assert _brief_row_count(db_conn, _USER, _SESSION, "open_cn") == 1


def test_is_idempotent_on_user_session_kind(db_conn: Connection) -> None:
    _seed_user(db_conn, _USER, "test-cn-open-idem@example.invalid")
    sector = _seed_cn_sector(db_conn, _USER, "ZZ-CN-O-2", None)
    _seed_holding(db_conn, _USER, sector, "ZZCNO9B.SS", "owned")

    for _ in range(2):
        assemble_cn_open_and_store(
            db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
        )

    assert _brief_row_count(db_conn, _USER, _SESSION, "open_cn") == 1


def test_sector_setup_reads_real_cn_bars(db_conn: Connection) -> None:
    _seed_user(db_conn, _USER, "test-cn-open-sectors@example.invalid")
    bench = "ZZCNOBENCH9.SS"
    sector = _seed_cn_sector(db_conn, _USER, "ZZ-CN-O-3", bench)
    _seed_holding(db_conn, _USER, sector, "ZZCNO9C.SS", "owned")
    _seed_bars(db_conn, bench, Decimal("100"), Decimal("1"), _PRIOR)

    obj = assemble_cn_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    setup = next(s for s in obj.sections if s.id.value == "sector_setup")
    row = next(r for r in setup.rows if r.benchmark_symbol == bench)
    # Closes 100..109; five sessions back from 109 is 104 -> 109/104 - 1.
    assert row.ret_5d is not None
    assert abs(row.ret_5d - (109 / 104 - 1)) < 1e-9
    # CN_BENCHMARK (000300.SS) is deliberately never *seeded* here (the shared
    # dev DB may already hold real committed rows for it from an earlier
    # backfill smoke test — never assumed absent), so `vs_spy_5d` is not
    # asserted either way; only the sector's own ZZ-prefixed benchmark return
    # is a value this test controls.


def test_always_sends_on_a_day_with_nothing_in_it(db_conn: Connection) -> None:
    _seed_user(db_conn, _USER, "test-cn-open-empty@example.invalid")
    sector = _seed_cn_sector(db_conn, _USER, "ZZ-CN-O-4", None)
    _seed_holding(db_conn, _USER, sector, "ZZCNO9D.SS", "owned")

    obj = assemble_cn_open_and_store(
        db_conn, _USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    assert obj is not None
    calendar = next(s for s in obj.sections if s.id.value == "calendar")
    assert calendar.tier.value == "suppressed"


# --- Cross-bleed: the two books' §4 never mix (change 2, CN-M2) -----------


def test_us_and_cn_open_briefs_never_bleed_calendar_symbols(db_conn: Connection) -> None:
    _seed_user(db_conn, _XBLEED_USER, "test-cn-xbleed@example.invalid")

    us_sector = str(
        db_conn.execute(
            text(
                "INSERT INTO sectors (user_id, name) VALUES (:u, 'ZZ-US-XBLEED') "
                "RETURNING id"
            ),
            {"u": _XBLEED_USER},
        ).scalar_one()
    )
    _seed_holding(db_conn, _XBLEED_USER, us_sector, "ZZUSXB9", "owned")

    cn_sector = _seed_cn_sector(db_conn, _XBLEED_USER, "ZZ-CN-XBLEED", None)
    _seed_holding(db_conn, _XBLEED_USER, cn_sector, "ZZCNXB9.SS", "owned")

    seed_events(db_conn, session_date=_SESSION, symbols=["ZZUSXB9", "ZZCNXB9.SS"])

    us_obj = assemble_open_and_store(
        db_conn, _XBLEED_USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )
    cn_obj = assemble_cn_open_and_store(
        db_conn, _XBLEED_USER, _SESSION, prior_session=_PRIOR, generated_at=_GENERATED_AT
    )

    us_calendar = next(s for s in us_obj.sections if s.id.value == "calendar")
    cn_calendar = next(s for s in cn_obj.sections if s.id.value == "calendar")
    us_symbols = {r.symbol for r in us_calendar.rows}
    cn_symbols = {r.symbol for r in cn_calendar.rows}

    assert "ZZUSXB9" in us_symbols
    assert "ZZCNXB9.SS" not in us_symbols  # the CN symbol never bleeds into the US §4

    assert "ZZCNXB9.SS" in cn_symbols
    assert "ZZUSXB9" not in cn_symbols  # the US symbol never bleeds into the CN §4
