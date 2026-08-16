"""compute_and_store's market/benchmark params (Task 4) against a real database
(skipped without DATABASE_URL): a mixed US+CN book filters cleanly by market,
the CN call's vs-benchmark figure reads the seeded benchmark's return, and the
exact Σ contribution_bps == book.day_bps identity (invariant 3, verify-numbers)
holds within the CN slice.

Symbols are ZZ-prefixed synthetic tickers, never real A-share symbols: the
shared dev DB may already hold committed rows for real CN tickers (e.g. from
worker_cn's backfill smoke test), and another session may run these tests
concurrently against the same DB, so a test must never assume a symbol is
absent from bars_daily/holdings — it has to own rows no other test or run
would ever write."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.compute import compute_and_store

# A dedicated throwaway user, distinct from other DB tests' fixtures — never
# reads or interacts with someone's actual book.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fd"

_PREV = date(2026, 8, 10)
_SESSION = date(2026, 8, 11)
_US_SECTOR = "ZZ-US-SECT"
_CN_SECTOR = "ZZ-CN-SECT"
_CN_SYM_1 = "ZZCN1.SS"
_CN_SYM_2 = "ZZCN2.SZ"
_CN_BENCHMARK = "ZZCNBM.SS"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-compute-cn@example.invalid')"),
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


def _seed_sector(conn: Connection, name: str, market: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO sectors (user_id, name, market) VALUES (:u, :n, :m) "
                "ON CONFLICT (user_id, name) DO UPDATE SET market = EXCLUDED.market "
                "RETURNING id"
            ),
            {"u": _TEST_USER_ID, "n": name, "m": market},
        ).scalar_one()
    )


def _seed_lot(
    conn: Connection, sector_id: str, symbol: str, shares: str, cost_cents: int
) -> None:
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


def _seed_mixed_book(conn: Connection) -> None:
    _seed_user(conn)

    us_sector = _seed_sector(conn, _US_SECTOR, "US")
    cn_sector = _seed_sector(conn, _CN_SECTOR, "CN")

    # US lot: ZZUS, prev 50 -> close 55.
    _seed_bar(conn, "ZZUS", _PREV, "50")
    _seed_bar(conn, "ZZUS", _SESSION, "55")
    _seed_lot(conn, us_sector, "ZZUS", "20", 4000)  # cost 40.00/share

    # CN lots: ZZCN1.SS up, ZZCN2.SZ down (fen cost bases).
    _seed_bar(conn, _CN_SYM_1, _PREV, "1800")
    _seed_bar(conn, _CN_SYM_1, _SESSION, "1830")
    _seed_lot(conn, cn_sector, _CN_SYM_1, "10", 170000)  # cost 1700.00/share

    _seed_bar(conn, _CN_SYM_2, _PREV, "200")
    _seed_bar(conn, _CN_SYM_2, _SESSION, "196")
    _seed_lot(conn, cn_sector, _CN_SYM_2, "50", 18000)  # cost 180.00/share

    # Synthetic benchmark stand-in, both sessions.
    _seed_bar(conn, _CN_BENCHMARK, _PREV, "4000")
    _seed_bar(conn, _CN_BENCHMARK, _SESSION, "4040")


def test_cn_market_filters_to_cn_symbols_only(db_conn: Connection) -> None:
    _seed_mixed_book(db_conn)

    result = compute_and_store(
        db_conn, _TEST_USER_ID, _SESSION, market="CN", benchmark=_CN_BENCHMARK
    )

    symbols = {p.symbol for p in result.positions}
    assert symbols == {_CN_SYM_1, _CN_SYM_2}
    assert "ZZUS" not in symbols


def test_cn_vs_benchmark_reads_csi_300_return(db_conn: Connection) -> None:
    _seed_mixed_book(db_conn)

    result = compute_and_store(
        db_conn, _TEST_USER_ID, _SESSION, market="CN", benchmark=_CN_BENCHMARK
    )

    # Seeded benchmark: 4000 -> 4040 is an exact +1% day.
    assert result.benchmark_return == Fraction(1, 100)

    day_bps = result.book.day_bps
    assert day_bps is not None
    expected_vs = day_bps - Fraction(1, 100) * 10_000
    assert result.book.vs_spy_bps == expected_vs


def test_cn_contribution_bps_sums_to_book_day_bps_exactly(db_conn: Connection) -> None:
    _seed_mixed_book(db_conn)

    result = compute_and_store(
        db_conn, _TEST_USER_ID, _SESSION, market="CN", benchmark=_CN_BENCHMARK
    )

    total_contribution = sum(
        (p.contribution_bps for p in result.positions if p.contribution_bps is not None),
        start=Fraction(0),
    )
    assert total_contribution == result.book.day_bps

    # Invariant 2 (verify-numbers): book P&L reconciles exactly, in cents.
    total_day_pnl = sum(p.day_pnl_cents for p in result.positions)
    assert total_day_pnl == result.book.day_pnl_cents


def test_default_market_sees_only_us_lot(db_conn: Connection) -> None:
    _seed_mixed_book(db_conn)

    result = compute_and_store(db_conn, _TEST_USER_ID, _SESSION)

    symbols = {p.symbol for p in result.positions}
    assert symbols == {"ZZUS"}
