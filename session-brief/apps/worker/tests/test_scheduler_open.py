"""The per-kind schedule (M14): two fires a session, on different anchors.

D20 built one self-rescheduling one-shot anchored on the real close, which is
what makes a half-day move the send automatically. The open fire cannot use that
anchor — 08:15 ET is a fixed wall-clock time that must *not* move on a half-day.
The DoD is exactly that asymmetry: a half-day moves the close and leaves the open
alone.

Pure — no clock, no network, no DB. Real NYSE 2026: Fri 2026-11-27 (the day after
Thanksgiving) is a 13:00 ET half-day; Mon 2026-09-07 is Labor Day.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from worker import scheduler

UTC = ZoneInfo("UTC")
_DELAY = timedelta(minutes=45)


# --- The open fire is wall-clock, not close-anchored ------------------------


def test_open_fire_is_0815_et() -> None:
    # 08:15 EDT == 12:15 UTC.
    assert scheduler.open_fire_time(date(2026, 9, 4)) == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)


def test_open_fire_survives_the_dst_boundary() -> None:
    """UTC at rest, America/New_York in logic (invariant 8). In November the
    offset is -5, so the same 08:15 ET is 13:15 UTC, not 12:15."""
    assert scheduler.open_fire_time(date(2026, 11, 30)) == datetime(
        2026, 11, 30, 13, 15, tzinfo=UTC
    )


def test_half_day_moves_the_close_fire_but_not_the_open() -> None:
    """The DoD. 2026-11-27 closes at 13:00 ET instead of 16:00."""
    half_day = date(2026, 11, 27)

    close_fire = scheduler.fire_time(half_day, _DELAY)
    open_fire = scheduler.open_fire_time(half_day)

    # Close moved three hours earlier: 13:45 ET == 18:45 UTC.
    assert close_fire == datetime(2026, 11, 27, 18, 45, tzinfo=UTC)
    # Open did not move: still 08:15 ET == 13:15 UTC.
    assert open_fire == datetime(2026, 11, 27, 13, 15, tzinfo=UTC)

    # And a normal session's open fires at the same wall-clock time. Both are
    # sessions, so neither lookup returns None — assert that before comparing.
    normal_open = scheduler.open_fire_time(date(2026, 11, 30))
    assert open_fire is not None and normal_open is not None
    assert (
        open_fire.astimezone(scheduler.ET).time()
        == normal_open.astimezone(scheduler.ET).time()
    )


# --- next_kind_fire: both fires, interleaved, in order ----------------------


def test_next_fire_returns_the_open_first_on_a_session_morning() -> None:
    now = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)  # 02:00 ET, before both
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "open"
    assert when == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)


def test_next_fire_returns_the_close_after_the_open_has_passed() -> None:
    now = datetime(2026, 9, 4, 13, 0, tzinfo=UTC)  # between the two
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "close"
    assert when == datetime(2026, 9, 4, 20, 45, tzinfo=UTC)


def test_after_fridays_close_the_next_fire_is_saturdays_heartbeat() -> None:
    """Saturday is not a session, so no open fire — but the close fire keeps its
    daily heartbeat so the dead-man's switch stays green over a weekend (D20)."""
    now = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)  # after Friday's close fire
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "close"
    assert when.date() == date(2026, 9, 5)  # Saturday


def test_the_next_open_after_a_holiday_weekend_is_tuesday() -> None:
    """Labor Day Monday 2026-09-07 is a holiday, so the first open brief of the
    week is Tuesday's — the close heartbeat fires on the holiday, the open
    doesn't."""
    now = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    opens = []
    for _ in range(6):
        when, kind = scheduler.next_kind_fire(now, _DELAY)
        if kind == "open":
            opens.append(when.date())
        now = when + timedelta(seconds=1)

    assert opens[0] == date(2026, 9, 8)  # Tuesday, not Sat/Sun/Labor Day


def test_open_does_not_fire_on_a_holiday() -> None:
    """The close fire keeps a daily heartbeat even on holidays (D20 — that is
    how the dead-man's switch stays green). The open brief has no such job, so
    it simply doesn't schedule on a non-session."""
    labor_day = date(2026, 9, 7)
    assert scheduler.open_fire_time(labor_day) is None


def test_a_session_gets_exactly_one_open_and_one_close() -> None:
    """Walk a full session's worth of fires and check the pair, in order."""
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    fires = []
    for _ in range(2):
        when, kind = scheduler.next_kind_fire(now, _DELAY)
        fires.append((when, kind))
        now = when + timedelta(seconds=1)

    assert [k for _, k in fires] == ["open", "close"]
    assert all(w.date() == date(2026, 9, 4) for w, _ in fires)
    assert fires[0][0] < fires[1][0]


# --- run_open_session_job: the 08:00 capture precedes assembly --------------
#
# A minimal engine stand-in: run_open_session_job only ever hands its
# connection to `ingest_premarket_for_session` and `assemble_open_and_store`,
# both monkeypatched below, so nothing here needs to talk to a real database.


class _StubTransaction:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _StubConn:
    def begin(self) -> _StubTransaction:
        return _StubTransaction()


class _StubEngine:
    def connect(self) -> contextlib.AbstractContextManager[_StubConn]:
        return contextlib.nullcontext(_StubConn())


def _engine() -> _StubEngine:
    return _StubEngine()


class _Stop(Exception):
    """Cuts the job short after the step under test — the ordering is the
    assertion, and delivery is somebody else's test."""


def test_the_open_job_captures_premarket_before_assembling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/02's staging, made real: the 08:15 send reads quotes the 08:00 stage
    wrote. M14's open job had nothing to ingest; M15 gives it the morning's."""
    calls: list[str] = []

    def fake_ingest(*_args: Any, **_kwargs: Any) -> int:
        calls.append("ingest")
        return 3

    def fake_assemble(*_args: Any, **_kwargs: Any) -> object:
        calls.append("assemble")
        raise _Stop

    monkeypatch.setattr(scheduler, "ingest_premarket_for_session", fake_ingest)
    monkeypatch.setattr("worker.assemble_open.assemble_open_and_store", fake_assemble)

    with pytest.raises(_Stop):
        scheduler.run_open_session_job(
            _engine(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 8, 13, 12, 15, tzinfo=UTC),
            healthcheck_url="",
        )
    assert calls == ["ingest", "assemble"]
