"""Live §4 calendar (M16): fdn's three calendar endpoints → `events`.

Replaces `events_seed` when FDN_API_KEY is set, one day-query per date in the
§4 window (the endpoints take a single `date`). The mapping targets exactly
the seed's vendor shape, so `assemble_open._calendar` needs no change. Lockup
expiries are covered by no vendor tier (docs/02) — in live mode they are
honestly absent rather than invented. Failures are per-endpoint and non-fatal:
§4 renders whatever fetched.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.engine import Engine

from worker.assemble_open import _CALENDAR_WINDOW_DAYS
from worker.constants import DEV_USER_ID
from worker.events_seed import _UPSERT, CalendarEvent
from worker.providers.fdn import FEED_ERRORS, FdnClient


def fetch_calendar_events(
    client: FdnClient, *, session_date: date, symbols: set[str]
) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for offset in range(_CALENDAR_WINDOW_DAYS + 1):
        d = session_date + timedelta(days=offset)
        events += _earnings(client, d, symbols)
        events += _ex_dividends(client, d, symbols)
        events += _macro(client, d)
    seen: set[tuple[str | None, str, date]] = set()
    unique: list[CalendarEvent] = []
    for e in events:
        key = (e.symbol, e.event_type, e.occurs_at)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _earnings(client: FdnClient, d: date, symbols: set[str]) -> list[CalendarEvent]:
    try:
        records = client.fetch("earnings-calendar", date=d.isoformat())
        return [
            CalendarEvent(
                sym, "earnings",
                date.fromisoformat(str(r["earnings_announcement_date"])),
                f"{sym} {r.get('fiscal_period') or ''} earnings".replace("  ", " "),
            )
            for r in records
            if (sym := str(r.get("trading_symbol"))) in symbols
            and r.get("earnings_announcement_date")
        ]
    except FEED_ERRORS:
        return []


def _ex_dividends(client: FdnClient, d: date, symbols: set[str]) -> list[CalendarEvent]:
    try:
        records = client.fetch("dividends-calendar", date=d.isoformat())
        return [
            CalendarEvent(
                sym, "ex_div", date.fromisoformat(str(r["ex_dividend_date"])),
                f"{sym} ex-dividend",
            )
            for r in records
            if (sym := str(r.get("trading_symbol"))) in symbols and r.get("ex_dividend_date")
        ]
    except FEED_ERRORS:
        return []


def _macro(client: FdnClient, d: date) -> list[CalendarEvent]:
    try:
        records = client.fetch("economic-calendar", date=d.isoformat())
        return [
            CalendarEvent(
                None, "macro", date.fromisoformat(str(r["release_date"])),
                str(r["indicator_name"]),
            )
            for r in records
            if str(r.get("country_code")) == "US"
            and r.get("release_date") and r.get("indicator_name")
        ]
    except FEED_ERRORS:
        return []


def ingest_events_for_session(
    engine: Engine, client: FdnClient, *, session_date: date, user_id: str = DEV_USER_ID
) -> int:
    """Fetch the window's calendars for the book's symbols and upsert into
    `events` on the seed's (symbol, event_type, occurs_at) key."""
    from worker.scheduler import book_symbols

    with engine.connect() as conn:
        held = set(book_symbols(conn, user_id))
    events = fetch_calendar_events(client, session_date=session_date, symbols=held)
    with engine.begin() as conn:
        for e in events:
            conn.execute(_UPSERT, {
                "symbol": e.symbol, "event_type": e.event_type,
                "occurs_at": e.occurs_at, "label": e.label,
            })
    return len(events)
