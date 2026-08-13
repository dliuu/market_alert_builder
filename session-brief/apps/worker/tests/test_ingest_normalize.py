"""M2 definition of done, against a real database (skipped without DATABASE_URL):
ingest is idempotent, and normalize replays raw_payloads into bars_daily
byte-for-byte."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars

_SYMBOL = "ZZTEST"


class _FakeProvider:
    """Returns canned bars — no network — while satisfying MarketDataProvider."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        return self._records if symbol.upper() == _SYMBOL else []

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError

    def latest_minute(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError

    def dividends(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError


def _record(session: str, close: str) -> dict[str, Any]:
    return {
        "date": f"{session}T00:00:00.000Z",
        "open": Decimal("10.0"),
        "high": Decimal("11.0"),
        "low": Decimal("9.5"),
        "close": Decimal(close),
        "volume": 12345,
        "adjClose": Decimal(close),
    }


def _snapshot(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT session_date, o, h, l, c, v, adj_c FROM bars_daily "
            "WHERE symbol = :s ORDER BY session_date"
        ),
        {"s": _SYMBOL},
    ).all()
    return [tuple(row) for row in rows]


def test_ingest_idempotent_and_normalize_replays_byte_for_byte(db_conn: Connection) -> None:
    provider = _FakeProvider([_record("2026-08-06", "100.11"), _record("2026-08-07", "101.22")])
    start, end = date(2026, 8, 1), date(2026, 8, 7)

    # Ingest writes one verbatim payload row; a second identical ingest is a no-op.
    assert ingest_daily_bars(db_conn, provider, [_SYMBOL], start, end) == 1
    assert ingest_daily_bars(db_conn, provider, [_SYMBOL], start, end) == 0

    payload_count = db_conn.execute(
        text("SELECT count(*) FROM raw_payloads WHERE symbol = :s"), {"s": _SYMBOL}
    ).scalar_one()
    assert payload_count == 1

    # Normalize fills bars_daily.
    assert normalize_bars(db_conn, [_SYMBOL]) == 2
    first = _snapshot(db_conn)
    assert [row[0].isoformat() for row in first] == ["2026-08-06", "2026-08-07"]
    assert first[1][4] == Decimal("101.22")  # close survives exactly

    # Replay: wipe bars_daily, normalize again from the untouched payloads.
    db_conn.execute(text("DELETE FROM bars_daily WHERE symbol = :s"), {"s": _SYMBOL})
    assert normalize_bars(db_conn, [_SYMBOL]) == 2
    assert _snapshot(db_conn) == first
