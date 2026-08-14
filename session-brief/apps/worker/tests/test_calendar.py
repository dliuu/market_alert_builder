"""Trading-calendar wrapper (M10). Pure — no DB, no network. Dates are checked
against real NYSE 2026: Labor Day (Mon 2026-09-07) is the holiday the M10 DoD
names, and the day after Thanksgiving (Fri 2026-11-27) is a 13:00 ET half-day."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from worker import calendar

UTC = ZoneInfo("UTC")


def test_is_session_weekday() -> None:
    assert calendar.is_session(date(2026, 9, 4)) is True  # Friday


def test_is_session_weekend() -> None:
    assert calendar.is_session(date(2026, 9, 5)) is False  # Saturday


def test_is_session_holiday() -> None:
    assert calendar.is_session(date(2026, 9, 7)) is False  # Labor Day


def test_session_close_regular_is_1600_et() -> None:
    close = calendar.session_close(date(2026, 9, 4))
    # 16:00 EDT == 20:00 UTC.
    assert close == datetime(2026, 9, 4, 20, 0, tzinfo=UTC)


def test_session_close_halfday_is_1300_et() -> None:
    close = calendar.session_close(date(2026, 11, 27))
    # Half-day: 13:00 EST == 18:00 UTC — three hours earlier than a normal close.
    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_session_close_rejects_non_session() -> None:
    try:
        calendar.session_close(date(2026, 9, 7))
    except ValueError:
        return
    raise AssertionError("expected session_close to reject a holiday")


def test_next_session_skips_weekend_and_holiday() -> None:
    # Friday 09-04 → next session skips Sat/Sun and Labor Day Mon → Tue 09-08.
    assert calendar.next_session(date(2026, 9, 4)) == date(2026, 9, 8)


def test_previous_session_walks_back_over_weekend() -> None:
    # 2026-09-07 is Labor Day (holiday); the prior session is Fri 2026-09-04.
    assert calendar.previous_session(date(2026, 9, 8)) == date(2026, 9, 4)


def test_today_et_rolls_back_across_midnight_utc() -> None:
    # 2026-09-05 01:30 UTC is still 2026-09-04 21:30 ET.
    assert calendar.today_et(datetime(2026, 9, 5, 1, 30, tzinfo=UTC)) == date(2026, 9, 4)


def test_close_or_standard_uses_real_close_on_session() -> None:
    assert calendar.close_or_standard(date(2026, 9, 4)) == calendar.session_close(date(2026, 9, 4))


def test_close_or_standard_uses_nominal_bell_off_session() -> None:
    # Labor Day (weekday holiday): nominal 16:00 ET bell so the heartbeat still lands.
    assert calendar.close_or_standard(date(2026, 9, 7)) == datetime(2026, 9, 7, 20, 0, tzinfo=UTC)


# --- previous_session: the open brief reads the prior close (M14) ---------


def test_previous_session_is_the_prior_trading_day() -> None:
    # Tue 2026-09-08 back to Fri 2026-09-04 — Labor Day Monday is skipped.
    assert calendar.previous_session(date(2026, 9, 8)) == date(2026, 9, 4)


def test_previous_session_from_a_monday_skips_the_weekend() -> None:
    assert calendar.previous_session(date(2026, 9, 14)) == date(2026, 9, 11)


def test_previous_session_is_strictly_before_a_session_date() -> None:
    """Called on a session, it must not return that same session — the open
    brief would otherwise read a close that hasn't happened yet."""
    d = date(2026, 9, 9)
    assert calendar.is_session(d) is True
    assert calendar.previous_session(d) < d
