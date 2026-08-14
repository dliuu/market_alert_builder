"""Trading calendar — the single source of truth for "is today a session?" and
"when does it close?" (invariant 7: never hardcode holidays or a 16:00 close).

Wraps ``exchange_calendars`` (XNYS = NYSE). Half-days close at 13:00 ET and the
close job must move with them (docs/02); ``session_close`` returns the real
close, so nothing downstream needs a holiday or half-day list.

UTC at rest, ``America/New_York`` in logic (invariant 8): the calendar library
returns tz-aware UTC timestamps, and the *session date* is always the ET
calendar date — ``today_et`` is how a UTC ``now`` becomes the right session.
"""

from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

EXCHANGE = "XNYS"  # NYSE. Both briefs are US-equity; one calendar covers them.
ET = ZoneInfo("America/New_York")

# Fallback close for a non-session day (weekend/holiday): the standard 16:00 ET
# bell. Only used to place the daily dead-man's-switch check-in on days that
# don't actually trade — never for a real send, which always uses session_close.
_STANDARD_CLOSE_ET = time(16, 0)


@lru_cache(maxsize=1)
def _calendar() -> xcals.ExchangeCalendar:
    return xcals.get_calendar(EXCHANGE)


def is_session(d: date) -> bool:
    """True iff ``d`` is a trading session (not a weekend or NYSE holiday)."""
    return bool(_calendar().is_session(d.isoformat()))


def session_close(d: date) -> datetime:
    """The tz-aware UTC close for session ``d`` — 16:00 ET normally, 13:00 ET on
    a half-day, DST handled by the calendar. Raises if ``d`` isn't a session."""
    if not is_session(d):
        raise ValueError(f"{d} is not a trading session")
    close: datetime = _calendar().session_close(d.isoformat()).to_pydatetime()
    return close


def next_session(d: date) -> date:
    """The first trading session strictly after ``d``."""
    nxt: date = _calendar().next_session(d.isoformat()).date()
    return nxt


def previous_session(d: date) -> date:
    """The last trading session strictly before ``d``. ``d`` need not itself be a
    session: the open brief reads it with a session ``D`` (at 08:15 every figure
    comes from D-1's close — M14), while the attribution weekend refit passes a
    non-session date (M13). ``exchange_calendars.previous_session`` requires a
    session as input, so a non-session ``d`` is rolled back via
    ``date_to_session`` first. "The day before" is not "yesterday": a Tuesday
    after a Monday holiday looks back to Friday."""
    cal = _calendar()
    if is_session(d):
        prev: date = cal.previous_session(d.isoformat()).date()
        return prev
    rolled: date = cal.date_to_session(d.isoformat(), direction="previous").date()
    return rolled


def today_et(now_utc: datetime) -> date:
    """The ET calendar date for a UTC instant — the session a run belongs to."""
    return now_utc.astimezone(ET).date()


def close_or_standard(d: date) -> datetime:
    """The tz-aware UTC send/heartbeat anchor for ``d``: the real session close on
    a trading day, else the nominal 16:00 ET bell so the daily check-in still
    lands at a predictable weekday time on a holiday."""
    if is_session(d):
        return session_close(d)
    return datetime.combine(d, _STANDARD_CLOSE_ET, tzinfo=ET).astimezone(ZoneInfo("UTC"))
