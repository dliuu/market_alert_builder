"""Live §4 calendar (M16): fdn's earnings/dividends/economic endpoints mapped
onto the `events_seed.CalendarEvent` shape. Two pure-function tests over a
mocked `FdnClient`, plus a `db_conn`-adjacent engine test for
`ingest_events_for_session`'s round-trip and idempotency (mirrors
`test_events_seed.py::test_seed_events_is_idempotent`).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
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
    events, failed = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert CalendarEvent("ZHELD", "earnings", date(2026, 8, 14), "ZHELD Q2 earnings") in events
    assert CalendarEvent("ZHELD", "ex_div", date(2026, 8, 17), "ZHELD ex-dividend") in events
    assert CalendarEvent(None, "macro", date(2026, 8, 14), "CPI (m/m)") in events
    symbols = {e.symbol for e in events}
    assert "ZOTHER" not in symbols                      # not in the book
    assert all(e.label != "ECB rate decision" for e in events)  # non-US macro dropped
    assert all(e.event_type != "lockup" for e in events)        # honestly absent live
    assert failed == []                                  # every endpoint succeeded


def test_a_failed_calendar_endpoint_degrades_to_what_fetched() -> None:
    """One endpoint's 500 must not suppress the other two's rows — an
    all-empty fixture can't distinguish "the others still contributed" from
    "everything happened to be empty," so this pairs the 500 with non-empty
    earnings/dividends bodies and asserts both survive while macro is absent.

    Fix A (M16 final review): the same 500 must also be *reported*, not just
    tolerated — `_read_events`/`_DELETE_WINDOW` can't distinguish "no macro
    releases this week" from "the endpoint was down," so `failed` is the
    reader's only way to tell. This is the endpoint-500-with-real-siblings
    case the fix asked for."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "economic-calendar":
            return httpx.Response(500)
        bodies = {
            "earnings-calendar": (
                '[{"trading_symbol": "ZHELD", "fiscal_period": "Q2",'
                '  "earnings_announcement_date": "2026-08-14"}]'
            ),
            "dividends-calendar": (
                '[{"trading_symbol": "ZHELD", "ex_dividend_date": "2026-08-17"}]'
            ),
        }
        return httpx.Response(200, text=bodies.get(endpoint, "[]"))

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    events, failed = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert CalendarEvent("ZHELD", "earnings", date(2026, 8, 14), "ZHELD Q2 earnings") in events
    assert CalendarEvent("ZHELD", "ex_div", date(2026, 8, 17), "ZHELD ex-dividend") in events
    assert all(e.event_type != "macro" for e in events)
    assert failed == ["calendar.economic"]


def test_a_malformed_earnings_body_degrades_like_a_failed_endpoint() -> None:
    """`FdnClient.fetch` raises `ValueError` on a non-list body, and a record
    that isn't a dict raises `AttributeError` from `r.get(...)` — both must
    degrade the earnings calendar exactly like a 500, not escape and kill the
    08:15 job (M16 review, finding 1)."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "earnings-calendar":
            return httpx.Response(200, text='["not-a-dict"]')
        return httpx.Response(200, text="[]")

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    events, failed = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert all(e.event_type != "earnings" for e in events)
    assert failed == ["calendar.earnings"]


def test_a_failed_earnings_endpoint_still_lets_macro_contribute_rows() -> None:
    """The converse direction: a 500 on earnings-calendar must not suppress a
    non-empty economic-calendar. Each helper (_earnings/_ex_dividends/_macro)
    wraps its own client.fetch in its own try/except with no shared state, so
    this exercises the same isolation mechanism from the other side."""
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "earnings-calendar":
            return httpx.Response(500)
        if endpoint == "economic-calendar":
            return httpx.Response(
                200,
                text=(
                    '[{"indicator_name": "CPI (m/m)", "country_code": "US",'
                    '  "release_date": "2026-08-14"}]'
                ),
            )
        return httpx.Response(200, text="[]")

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    events, failed = fetch_calendar_events(client, session_date=_SESSION, symbols={"ZHELD"})
    assert CalendarEvent(None, "macro", date(2026, 8, 14), "CPI (m/m)") in events
    assert all(e.event_type != "earnings" for e in events)
    assert failed == ["calendar.earnings"]


# --- ingest_events_for_session: DB round-trip + idempotency ----------------

# The DB tests run against the dev database, and `ingest_events_for_session`
# now *replaces* its date window (finding 2) rather than merging into it — so
# a test session date inside the real calendar would delete the dev book's
# coming week of §4 rows. Far-future, per `test_scheduler_fdn.py`'s
# `_LIVE_SESSION`, keeps the blast radius inside the test's own rows.
_DB_SESSION = date(2099, 3, 15)


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
            '  "earnings_announcement_date": "2099-03-15"}]'
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

        first_written, first_failed = ingest_events_for_session(
            engine, client, session_date=_DB_SESSION, user_id=user_id
        )
        assert first_written == 1
        assert first_failed == []

        with engine.connect() as conn:
            first = _rows(conn, label_like="ZI6A%")
        assert first == [{
            "symbol": "ZI6A", "event_type": "earnings",
            "occurs_at": _DB_SESSION, "label": "ZI6A Q2 earnings",
        }]

        second_written, second_failed = ingest_events_for_session(
            engine, client, session_date=_DB_SESSION, user_id=user_id
        )
        assert second_written == 1
        assert second_failed == []

        with engine.connect() as conn:
            second = _rows(conn, label_like="ZI6A%")
        assert second == first
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM events WHERE symbol = 'ZI6A'"))
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_live_ingest_replaces_the_window_and_spares_rows_outside_it(engine: Engine) -> None:
    """Switch-on data hygiene (M16 final review, finding 2). The `events` table
    has been seeded synthetically since M14 — `events_seed._PER_SYMBOL` invents
    a "{symbol} lockup expiry" at +6d — and `_read_events` has no provenance
    column to filter on, so a merging ingest would render invented lockups in a
    brief whose synthetic-feed banner is *off*. The live ingest must therefore
    replace the window, while leaving history and anything past the horizon
    alone (a truncate would be its own bug)."""
    from worker.assemble_open import _CALENDAR_WINDOW_DAYS

    user_id = str(uuid4())
    session = _DB_SESSION
    inside = session + timedelta(days=6)                        # the seed's lockup slot
    outside = session + timedelta(days=_CALENDAR_WINDOW_DAYS + 1)
    client = _client({
        "earnings-calendar": (
            '[{"trading_symbol": "ZI6B", "fiscal_period": "Q2",'
            '  "earnings_announcement_date": "2099-03-15"}]'
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
                     "VALUES (:u, :sec, 'ZI6B', 'owned')"),
                {"u": user_id, "sec": sector_id},
            )
            # A synthetic-style row inside the window, and one beyond it.
            for occurs_at, label in ((inside, "ZI6B lockup expiry"),
                                     (outside, "ZI6B lockup expiry (outside)")):
                conn.execute(
                    text("INSERT INTO events (symbol, event_type, occurs_at, label) "
                         "VALUES ('ZI6B', 'lockup', :d, :label)"),
                    {"d": occurs_at, "label": label},
                )

        ingest_events_for_session(engine, client, session_date=session, user_id=user_id)

        with engine.connect() as conn:
            rows = _rows(conn, label_like="ZI6B%")
        occurrences = {(r["event_type"], r["occurs_at"]) for r in rows}
        assert ("lockup", inside) not in occurrences        # purged with the window
        assert ("lockup", outside) in occurrences           # untouched beyond it
        assert ("earnings", session) in occurrences         # the live row landed
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM events WHERE symbol = 'ZI6B'"))
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
