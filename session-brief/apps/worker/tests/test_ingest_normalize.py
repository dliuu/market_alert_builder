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

    def dividends_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError

    def economic_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
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


def test_widening_the_window_deepens_history(db_conn: Connection) -> None:
    """A wider re-fetch ending on the same date must not be read as a duplicate.

    The regression: ``raw_payloads`` deduped on ``(source, endpoint, symbol,
    as_of)`` where ``as_of`` is the payload's *newest* session, so a 260-day
    backfill and a 10-day poll ending the same day collided and the deeper
    history was silently dropped. That capped SPY at 65 sessions and the
    attribution fit at 64 observations instead of 120 (2026-08-14).
    """
    end = date(2026, 8, 7)
    narrow = _FakeProvider([_record("2026-08-06", "100.11"), _record("2026-08-07", "101.22")])
    assert ingest_daily_bars(db_conn, narrow, [_SYMBOL], date(2026, 8, 1), end) == 1
    assert normalize_bars(db_conn, [_SYMBOL]) == 2

    # Same end date, more history. Same (source, endpoint, symbol, as_of) tuple.
    wide = _FakeProvider([
        _record("2026-07-30", "98.00"),
        _record("2026-07-31", "99.00"),
        _record("2026-08-06", "100.11"),
        _record("2026-08-07", "101.22"),
    ])
    assert ingest_daily_bars(db_conn, wide, [_SYMBOL], date(2026, 7, 1), end) == 1

    assert normalize_bars(db_conn, [_SYMBOL]) == 4
    assert [row[0].isoformat() for row in _snapshot(db_conn)] == [
        "2026-07-30", "2026-07-31", "2026-08-06", "2026-08-07",
    ]

    # The narrow payload is still there, unmutated — normalize replays both
    # (invariant 5). Two rows, distinguished only by body content.
    assert db_conn.execute(
        text("SELECT count(*) FROM raw_payloads WHERE symbol = :s"), {"s": _SYMBOL}
    ).scalar_one() == 2

    # ...and re-fetching either window is still a no-op, so the 90s bar poll
    # does not accumulate a row per fetch.
    assert ingest_daily_bars(db_conn, wide, [_SYMBOL], date(2026, 7, 1), end) == 0
    assert ingest_daily_bars(db_conn, narrow, [_SYMBOL], date(2026, 8, 1), end) == 0


def test_revision_within_an_identical_window_is_a_known_noop(db_conn: Connection) -> None:
    """Documents the limitation this key deliberately does not close.

    A vendor revising a bar *inside* an unchanged window still no-ops, exactly as
    it did before the key was widened. Catching that needs a content hash, which
    Postgres will not index (``body::text`` is STABLE, not IMMUTABLE). Asserted
    so the behaviour is a recorded decision rather than a surprise.
    """
    window = (date(2026, 8, 1), date(2026, 8, 7))
    original = _FakeProvider([_record("2026-08-07", "101.22")])
    revised = _FakeProvider([_record("2026-08-07", "999.99")])  # same window, new close

    assert ingest_daily_bars(db_conn, original, [_SYMBOL], *window) == 1
    assert ingest_daily_bars(db_conn, revised, [_SYMBOL], *window) == 0  # not stored

    assert normalize_bars(db_conn, [_SYMBOL]) == 1
    assert _snapshot(db_conn)[0][4] == Decimal("101.22")  # the original survives
