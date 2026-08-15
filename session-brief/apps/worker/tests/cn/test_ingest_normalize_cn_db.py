"""Source-scoped ingest/normalize against a real database (skipped without
DATABASE_URL): synthetic-cn payloads are namespaced by source, replay from
raw_payloads reproduces bars_daily byte-for-byte (the D13 replay property),
and a default (Tiingo-scoped) normalize_bars() never consumes them.

Uses a ZZ-prefixed synthetic symbol, never a real A-share ticker: the shared
dev DB already holds committed synthetic-cn rows for real tickers (e.g.
600519.SS, from worker_cn's own backfill smoke test), and another session may
run this file concurrently against the same DB, so the test must own a symbol
no other test or run would ever write — the provider is deterministic for any
symbol string, so this exercises identical behavior."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars
from worker_cn.providers import SyntheticCnBarsProvider

_SYMBOL = "ZZCNI.SS"
_SOURCE = "synthetic-cn"
_ENDPOINT = "daily/prices"

_START = date(2026, 8, 3)
_END = date(2026, 8, 14)


def _snapshot(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT session_date, o, h, l, c, v, adj_c FROM bars_daily "
            "WHERE symbol = :s ORDER BY session_date"
        ),
        {"s": _SYMBOL},
    ).all()
    return [tuple(row) for row in rows]


def test_synthetic_cn_round_trip_and_replay(db_conn: Connection) -> None:
    provider = SyntheticCnBarsProvider()

    written = ingest_daily_bars(
        db_conn, provider, [_SYMBOL], _START, _END, source=_SOURCE, endpoint=_ENDPOINT
    )
    assert written == 1

    row = db_conn.execute(
        text("SELECT source, endpoint FROM raw_payloads WHERE symbol = :s"), {"s": _SYMBOL}
    ).mappings().one()
    assert row["source"] == "synthetic-cn"
    assert row["endpoint"] == "daily/prices"

    bars_written = normalize_bars(db_conn, [_SYMBOL], sources=(_SOURCE,))
    assert bars_written > 0
    first = _snapshot(db_conn)
    assert first  # at least one XSHG session in the window
    assert all(isinstance(row[4], Decimal) for row in first)  # close is Decimal at rest

    # D13 replay property: wipe bars_daily, replay from the untouched payload.
    db_conn.execute(text("DELETE FROM bars_daily WHERE symbol = :s"), {"s": _SYMBOL})
    assert normalize_bars(db_conn, [_SYMBOL], sources=(_SOURCE,)) == bars_written
    assert _snapshot(db_conn) == first


def test_default_normalize_does_not_consume_synthetic_cn(db_conn: Connection) -> None:
    provider = SyntheticCnBarsProvider()
    ingest_daily_bars(
        db_conn, provider, [_SYMBOL], _START, _END, source=_SOURCE, endpoint=_ENDPOINT
    )

    assert normalize_bars(db_conn, [_SYMBOL]) == 0
    assert _snapshot(db_conn) == []
