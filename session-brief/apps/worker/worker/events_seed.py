"""Synthetic calendar seeding for the open brief's §4 (M14).

Nothing has ever written to `events` — M7 created it for the lockup flag and
seeded it by hand (D18). §4 "On the clock today" needs a populated calendar, and
the live source is licensing-gated: fdnpy exposes `get_earnings_calendar`,
`get_dividends_calendar` and `get_economic_calendar`, but they are Premium tier
and redistribution needs a commercial plan (docs/02, D8) — the same gate M15
defers. So M14 seeds, exactly as M7 did for `fundamentals`.

The rows are emitted in the **vendor calendar shape** (`symbol`, `event_type`,
`occurs_at`, `label`) so swapping to a live feed is a provider call, not a
reshape of §4.

Seeding is idempotent on `(symbol, event_type, occurs_at)` — re-running replaces
rather than duplicating, so it is safe to wire into a scheduled pre-open job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class CalendarEvent:
    """One row of §4. `symbol` is None for a macro release, which names no
    security — the reason the M7 NOT NULL had to go."""

    symbol: str | None
    event_type: str
    occurs_at: date
    label: str


# Macro releases, as offsets from the session date. Deterministic so a seeded
# calendar is reproducible in a snapshot test; the labels mirror what an
# economic-calendar feed returns.
_MACRO: tuple[tuple[int, str], ...] = (
    (0, "CPI (m/m)"),
    (1, "Initial jobless claims"),
    (2, "FOMC rate decision"),
)

# Per-symbol events, as (offset, type, label template). Spread across the window
# so §4 has something on the session date itself and something ahead of it.
_PER_SYMBOL: tuple[tuple[int, str, str], ...] = (
    (0, "earnings", "{symbol} Q2 earnings"),
    (3, "ex_div", "{symbol} ex-dividend"),
    (6, "lockup", "{symbol} lockup expiry"),
)


def build_events(session_date: date, symbols: list[str]) -> list[CalendarEvent]:
    """The calendar to seed, pure and deterministic. Macro rows first, then each
    symbol's events in the given order — so the caller can snapshot it."""
    events = [
        CalendarEvent(None, "macro", session_date + timedelta(days=offset), label)
        for offset, label in _MACRO
    ]
    for symbol in symbols:
        events += [
            CalendarEvent(
                symbol, event_type, session_date + timedelta(days=offset),
                template.format(symbol=symbol),
            )
            for offset, event_type, template in _PER_SYMBOL
        ]
    return events


# --- Database layer -------------------------------------------------------

_UPSERT = text("""
    INSERT INTO events (symbol, event_type, occurs_at, label)
    VALUES (:symbol, :event_type, :occurs_at, :label)
    ON CONFLICT (symbol, event_type, occurs_at) DO UPDATE
        SET label = EXCLUDED.label
""")


def seed_events(conn: Connection, *, session_date: date, symbols: list[str]) -> int:
    """Write the synthetic calendar and return the number of rows touched."""
    events = build_events(session_date, symbols)
    for event in events:
        conn.execute(
            _UPSERT,
            {
                "symbol": event.symbol,
                "event_type": event.event_type,
                "occurs_at": event.occurs_at,
                "label": event.label,
            },
        )
    return len(events)
