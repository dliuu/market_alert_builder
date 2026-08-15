"""Tests for the Chinese-side (XSHG) trading calendar. Pure — no DB, no network.
Dates are checked against real XSHG 2026: National Day / Golden Week
(2026-10-01..07) and Chinese New Year (2026-02-16..23), both verified against
``exchange_calendars`` before being pinned here."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from worker import calendar
from worker_cn.calendar import CN

UTC = ZoneInfo("UTC")


def test_is_session_false_on_national_day() -> None:
    assert CN.is_session(date(2026, 10, 1)) is False  # National Day (Golden Week)


def test_is_session_false_on_chinese_new_year() -> None:
    assert CN.is_session(date(2026, 2, 17)) is False  # Chinese New Year


def test_is_session_true_on_a_plain_friday() -> None:
    assert CN.is_session(date(2026, 8, 14)) is True


def test_session_close_is_1500_cst() -> None:
    # 15:00 CST == 07:00 UTC (Asia/Shanghai has no DST).
    assert CN.session_close(date(2026, 8, 14)) == datetime(2026, 8, 14, 7, 0, tzinfo=UTC)


def test_local_date_boundary_us_vs_cn() -> None:
    # 2026-08-14T17:30:00Z is 13:30 ET (still the 14th) but 01:30 CST the next day.
    now_utc = datetime(2026, 8, 14, 17, 30, tzinfo=UTC)
    assert calendar.US.local_date(now_utc) == date(2026, 8, 14)
    assert CN.local_date(now_utc) == date(2026, 8, 15)


def test_previous_session_crosses_golden_week() -> None:
    # 2026 National Day / Golden Week break: last session before it is 2026-09-30,
    # first session after it is 2026-10-08 (verified against exchange_calendars).
    assert CN.previous_session(date(2026, 10, 8)) == date(2026, 9, 30)


def test_module_delegation_matches_us_instance_is_session() -> None:
    session_day = date(2026, 9, 4)  # Friday
    holiday = date(2026, 9, 7)  # Labor Day
    assert calendar.is_session(session_day) == calendar.US.is_session(session_day)
    assert calendar.is_session(holiday) == calendar.US.is_session(holiday)


def test_module_delegation_matches_us_instance_today_et() -> None:
    now_utc = datetime(2026, 9, 5, 1, 30, tzinfo=UTC)
    assert calendar.today_et(now_utc) == calendar.US.local_date(now_utc)
