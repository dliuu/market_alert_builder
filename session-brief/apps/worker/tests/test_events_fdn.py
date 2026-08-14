"""Live §4 calendar (M16): fdn's earnings/dividends/economic endpoints mapped
onto the `events_seed.CalendarEvent` shape. Two pure-function tests over a
mocked `FdnClient`, plus a `db_conn`-adjacent engine test for
`ingest_events_for_session`'s round-trip and idempotency (mirrors
`test_events_seed.py::test_seed_events_is_idempotent`).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from worker.events_fdn import fetch_calendar_events, ingest_events_for_session
from worker.events_seed import CalendarEvent
from worker.providers.fdn import FdnClient

_SESSION = date(2026, 8, 14)


def _client(responses: dict[str, str]) -> FdnClient:
    """Route by endpoint path; any date param gets the same canned body."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=responses.get(endpoint, "[]"))
    return FdnClient("k", transport=httpx.MockTransport(handler))


def test_calendar_events_map_to_the_seed_shape_and_filter_to_the_book() -> None:
    client = _client({
        "earnings-calendar": (
            '[{"trading_symbol": "ZHELD", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2026-08-14"},'
            ' {"trading_symbol": "ZOTHER", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2026-08-14"}]'
        ),
        "dividends-calendar": (
            '[{"trading_symbol": "ZHELD", "ex_dividend_date": "2026-08-17"}]'
        ),
        "economic-calendar": (
            '[{"indicator_name": "CPI (m/m)", "country_code": "US",'
            '  "release_date": "2026-08-14"},'
            ' {"indicator_name": "ECB rate decision", "country_code": "EU",'
            '  "release_date": "2026-08-14"}]'
        ),
    })
    events = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert CalendarEvent("ZHELD", "earnings", date(2026, 8, 14), "ZHELD Q2 earnings") in events
    assert CalendarEvent("ZHELD", "ex_div", date(2026, 8, 17), "ZHELD ex-dividend") in events
    assert CalendarEvent(None, "macro", date(2026, 8, 14), "CPI (m/m)") in events
    symbols = {e.symbol for e in events}
    assert "ZOTHER" not in symbols                      # not in the book
    assert all(e.label != "ECB rate decision" for e in events)  # non-US macro dropped
    assert all(e.event_type != "lockup" for e in events)        # honestly absent live


def test_a_failed_calendar_endpoint_degrades_to_what_fetched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("economic-calendar"):
            return httpx.Response(500)
        return httpx.Response(200, text="[]")

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    assert fetch_calendar_events(client, session_date=_SESSION, symbols=set()) == []


# --- ingest_events_for_session: DB round-trip + idempotency ----------------


def _rows(conn: Connection, *, label_like: str) -> list[dict[str, object]]:
    return [
        dict(r)
        for r in conn.execute(
            text(
                "SELECT symbol, event_type, occurs_at, label FROM events "
                "WHERE label LIKE :label "
                "ORDER BY occurs_at, event_type, symbol NULLS FIRST"
            ),
            {"label": label_like},
        ).mappings()
    ]


@pytest.fixture
def engine() -> Iterator[Engine]:
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    yield get_engine()


def test_ingest_events_for_session_round_trips_and_is_idempotent(engine: Engine) -> None:
    user_id = str(uuid4())
    client = _client({
        "earnings-calendar": (
            '[{"trading_symbol": "ZI6A", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2026-08-14"}]'
        ),
    })
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": user_id, "e": f"{user_id}@example.invalid"},
            )
            sector_id = conn.execute(
                text("INSERT INTO sectors (user_id, name) VALUES (:u, 'M16 Test') "
                     "RETURNING id"),
                {"u": user_id},
            ).scalar()
            conn.execute(
                text("INSERT INTO holdings (user_id, sector_id, symbol, status) "
                     "VALUES (:u, :sec, 'ZI6A', 'owned')"),
                {"u": user_id, "sec": sector_id},
            )

        first_written = ingest_events_for_session(
            engine, client, session_date=_SESSION, user_id=user_id
        )
        assert first_written == 1

        with engine.connect() as conn:
            first = _rows(conn, label_like="ZI6A%")
        assert first == [{
            "symbol": "ZI6A", "event_type": "earnings",
            "occurs_at": _SESSION, "label": "ZI6A Q2 earnings",
        }]

        second_written = ingest_events_for_session(
            engine, client, session_date=_SESSION, user_id=user_id
        )
        assert second_written == 1

        with engine.connect() as conn:
            second = _rows(conn, label_like="ZI6A%")
        assert second == first
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM events WHERE symbol = 'ZI6A'"))
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
