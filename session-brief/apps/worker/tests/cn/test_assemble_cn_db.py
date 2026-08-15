"""``assemble_cn_close_and_store`` end-to-end against a real database (skipped
without DATABASE_URL): compute -> tape -> the pure shared ``assemble()`` ->
upsert, exercised through actual CN sector/holdings/lots rows and ~45 sessions
of ingested+normalized synthetic bars.

Symbols are ZZ-prefixed synthetic tickers, never real A-share symbols, per the
convention `024f3c0` established: the shared dev DB already holds committed
rows for real CN tickers (600519.SS, 300750.SZ, 000300.SS — 64 sessions each,
from an earlier backfill smoke test), so a DB test must own symbols no other
test or run would ever write. The CN benchmark constant
(``worker_cn.constants.CN_BENCHMARK`` = ``000300.SS``) is not seeded here at
all — ``compute_and_store`` degrades a missing benchmark price to
``benchmark_return=None`` rather than raising, so this test never touches that
real symbol's rows, seeded or not."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars
from worker_cn.assemble import assemble_cn_close_and_store
from worker_cn.providers import SyntheticCnBarsProvider

_TEST_USER_ID = "00000000-0000-0000-0000-0000000000c7"
_EMPTY_BOOK_USER_ID = "00000000-0000-0000-0000-0000000000c8"

_CN_SECTOR = "ZZ-CN-SECT-7"
_SYM_FULL = "ZZCNA7.SS"  # -1.79% on the session date -> full-tier mover
_SYM_QUIET = "ZZCNB7.SZ"  # -0.02% on the session date -> suppressed

_SOURCE = "synthetic-cn"
_ENDPOINT = "daily/prices"

# 54 XSHG sessions -> well over the 30 prior sessions RVOL needs, ending on the
# session under test.
_INGEST_START = date(2026, 6, 1)
_SESSION = date(2026, 8, 14)


def _seed_user(conn: Connection, user_id: str, email: str) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, :e)"),
        {"u": user_id, "e": email},
    )


def _seed_sector(conn: Connection, user_id: str, name: str, market: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO sectors (user_id, name, market) VALUES (:u, :n, :m) "
                "ON CONFLICT (user_id, name) DO UPDATE SET market = EXCLUDED.market "
                "RETURNING id"
            ),
            {"u": user_id, "n": name, "m": market},
        ).scalar_one()
    )


def _seed_lot(
    conn: Connection, user_id: str, sector_id: str, symbol: str, shares: str, cost_cents: int
) -> None:
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": user_id, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, :sh, :cb, :o)"
        ),
        {
            "u": user_id,
            "h": holding_id,
            "sh": Decimal(shares),
            "cb": cost_cents,
            "o": date(2026, 8, 1),
        },
    )


def _seed_book(conn: Connection) -> None:
    _seed_user(conn, _TEST_USER_ID, "test-assemble-cn@example.invalid")
    sector = _seed_sector(conn, _TEST_USER_ID, _CN_SECTOR, "CN")
    _seed_lot(conn, _TEST_USER_ID, sector, _SYM_FULL, "10", 100_000)  # cost 1000.00/share
    _seed_lot(conn, _TEST_USER_ID, sector, _SYM_QUIET, "5", 130_000)  # cost 1300.00/share

    provider = SyntheticCnBarsProvider()
    for symbol in (_SYM_FULL, _SYM_QUIET):
        ingest_daily_bars(
            conn, provider, [symbol], _INGEST_START, _SESSION,
            source=_SOURCE, endpoint=_ENDPOINT,
        )
    normalize_bars(conn, [_SYM_FULL, _SYM_QUIET], sources=(_SOURCE,))


def _brief_row_count(conn: Connection, user_id: str, session_date: date) -> int:
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM briefs "
                "WHERE user_id = :u AND session_date = :d AND kind = 'close_cn'"
            ),
            {"u": user_id, "d": session_date},
        ).scalar_one()
    )


def test_assemble_cn_close_and_store_writes_one_row(db_conn: Connection) -> None:
    _seed_book(db_conn)

    obj = assemble_cn_close_and_store(db_conn, _TEST_USER_ID, _SESSION)

    assert obj is not None  # ZZCNA7.SS moved -1.79% -> a full-tier mover, so it sends
    assert obj.kind.value == "close_cn"
    assert _brief_row_count(db_conn, _TEST_USER_ID, _SESSION) == 1


def test_assemble_cn_close_and_store_is_idempotent(db_conn: Connection) -> None:
    _seed_book(db_conn)

    assemble_cn_close_and_store(db_conn, _TEST_USER_ID, _SESSION)
    assemble_cn_close_and_store(db_conn, _TEST_USER_ID, _SESSION)  # re-assembly replaces

    assert _brief_row_count(db_conn, _TEST_USER_ID, _SESSION) == 1


def test_empty_cn_book_raises(db_conn: Connection) -> None:
    _seed_user(db_conn, _EMPTY_BOOK_USER_ID, "test-assemble-cn-empty@example.invalid")
    # No sector, no holdings, no lots for this user: an empty CN book.

    with pytest.raises(ValueError, match="no CN holdings"):
        assemble_cn_close_and_store(db_conn, _EMPTY_BOOK_USER_ID, _SESSION)
