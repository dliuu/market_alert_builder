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

from worker import calendar, config, scheduler

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

    # And a normal session's open fires at the same wall-clock time.
    normal_open = scheduler.open_fire_time(date(2026, 11, 30))
    assert (
        open_fire.astimezone(scheduler.ET).time()
        == normal_open.astimezone(scheduler.ET).time()
    )


# --- next_kind_fire: both fires, interleaved, in order ----------------------


def test_next_fire_returns_the_open_first_on_a_session_morning() -> None:
    # 04:00 ET, before both US fires and after CN's close_cn heartbeat (07:20
    # UTC) has already passed for the day — CN-M2 interleaves two more kinds
    # into this same walk (see tests/cn/test_scheduler_cn.py), so 08:00 UTC
    # rather than 06:00 UTC keeps this test about the US pair specifically.
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "open"
    assert when == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)


def test_next_fire_returns_the_close_after_the_open_has_passed() -> None:
    now = datetime(2026, 9, 4, 13, 0, tzinfo=UTC)  # between the two
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "close"
    assert when == datetime(2026, 9, 4, 20, 45, tzinfo=UTC)


def test_after_fridays_close_the_next_fire_is_saturdays_open_heartbeat() -> None:
    """Both fires now keep a daily heartbeat (the 2026-08-15 fix): Saturday's
    08:15 ET open candidate lands before its 16:45 ET close candidate, so it's
    the next *US* fire — not Monday, and not a gap until Monday.

    Before this fix, ``open_fire_time`` returned ``None`` on a non-session day,
    so ``next_kind_fire`` never produced an "open" candidate on a weekend or
    holiday at all — ``run_open_session_job`` was simply never invoked, and its
    own (already correct) is_session-check-and-ping-success never ran.
    ``HEALTHCHECKS_OPEN_URL`` went dark for the entire weekend as a result:
    observed 2026-08-15, no ping landed between Friday's real open send and the
    following Monday. Exactly the "weekday-only cron reads a holiday as a
    failed check-in" anti-pattern D20 built the close job to avoid — just not
    applied symmetrically here until now.

    CN-M2 note: starting from Saturday 08:00 UTC rather than straight after
    Friday's US close (21:00 UTC Friday) — the literal next fire at that
    instant is CN's own close_cn heartbeat (07:20 UTC Saturday), which fires
    daily independent of the US calendar (tests/cn/test_scheduler_cn.py covers
    that four-way interleaving directly). This test stays scoped to proving
    the US open heartbeat is the next *US* fire.
    """
    now = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)  # Saturday, after CN's own heartbeat
    when, kind = scheduler.next_kind_fire(now, _DELAY)
    assert kind == "open"
    assert when == datetime(2026, 9, 5, 12, 15, tzinfo=UTC)  # Sat 08:15 ET


def test_an_open_heartbeat_fires_every_day_including_a_holiday() -> None:
    """The schedule no longer skips "open" candidates on non-sessions — every
    calendar day gets one, mirroring the close heartbeat (D20). Whether a given
    fire actually *sends* is the job body's call (``run_open_session_job``'s own
    is_session check), not the scheduler's.

    CN-M2 interleaves two more kinds into the same walk (CN's open_cn fires on
    XSHG sessions only, close_cn every day) — this test filters to "open" only,
    same as before, but needs 13 fires rather than 8 to reach four of them now
    that two more candidates land most days."""
    now = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    opens = []
    for _ in range(13):
        when, kind = scheduler.next_kind_fire(now, _DELAY)
        if kind == "open":
            opens.append(when.date())
        now = when + timedelta(seconds=1)

    assert opens == [
        date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7),  # Sat, Sun, Labor Day
        date(2026, 9, 8),  # the next real session
    ]


def test_open_fire_time_lands_on_a_holiday_too() -> None:
    """The regression test: a nominal heartbeat time, not ``None``, on a
    non-session day — see the docstring on ``open_fire_time`` for the full
    story of why this matters."""
    labor_day = date(2026, 9, 7)
    assert not calendar.is_session(labor_day)
    assert scheduler.open_fire_time(labor_day) == datetime(2026, 9, 7, 12, 15, tzinfo=UTC)


def test_a_holiday_open_heartbeat_reaches_the_skip_and_ping_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the scheduled holiday fire actually invokes the job, and the
    job's own is_session guard is what pings success — not a scheduler no-op."""
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    labor_day_0815_et = datetime(2026, 9, 7, 12, 15, tzinfo=UTC)
    outcome = scheduler.run_open_session_job(
        _engine(),  # type: ignore[arg-type]
        now_utc=labor_day_0815_et,
        healthcheck_url="https://hc.example/open",
    )
    assert outcome == "skipped-holiday"
    assert pings == [("ok", "https://hc.example/open")]


def test_a_session_gets_exactly_one_open_and_one_close() -> None:
    """Walk a full session's worth of fires and check the US pair, in order.

    CN-M2 interleaves two more kinds (open_cn, close_cn) into the same walk on
    a shared session day — see
    ``tests/cn/test_scheduler_cn.py::test_a_full_shared_session_day_yields_four_fires_in_order``
    for the four-way version — so this walks enough fires to collect the US
    pair and filters to it explicitly, same as before CN-M2.
    """
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    fires = []
    for _ in range(4):
        when, kind = scheduler.next_kind_fire(now, _DELAY)
        if kind in ("open", "close"):
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
# `begin()` is included alongside `connect()` (M16) — the live-client branch
# takes an `engine.begin()` of its own around `store_captured_payloads`.


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

    def begin(self) -> contextlib.AbstractContextManager[_StubConn]:
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
    wrote. M14's open job had nothing to ingest; M15 gives it the morning's.

    `FDN_API_KEY` is pinned unset here (M16 review, finding 2) rather than left
    to whatever happens to be in the environment when the suite runs — the
    keyless path is an assertion (`client is None`), not an accident of a blank
    `.env`. The keyed path gets its own test below.
    """
    monkeypatch.setattr(config, "FDN_API_KEY", "")
    calls: list[str] = []
    seen_clients: list[Any] = []

    def fake_ingest(*_args: Any, client: Any = None, **_kwargs: Any) -> int:
        calls.append("ingest")
        seen_clients.append(client)
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
    assert seen_clients == [None]  # keyless: no FdnClient ever constructed


def test_the_open_job_wires_the_live_client_when_a_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M16 review, finding 2: the keyed path — the entire point of this
    milestone — had no coverage, so it silently depended on the real
    `FdnClient` constructor, `book_symbols` against a real connection, and a
    real vendor call the moment `FDN_API_KEY` is set. Every collaborator that
    would otherwise touch the network or a real database is faked at the same
    module-attribute boundary the keyless test above uses, so this stays
    hermetic while still proving the calendar/news wiring actually runs and
    that the fetched headlines reach `assemble_open_and_store` as `news`.
    """
    monkeypatch.setattr(config, "FDN_API_KEY", "test-key")
    monkeypatch.setattr(scheduler, "book_symbols", lambda *_a, **_kw: ["ZHELD"])

    calls: list[str] = []

    class _FakeClient:
        """Never a real FdnClient; identity is all the wiring assertions check.
        `close` is recorded because the job must release the connection pool it
        opened — the worker is long-lived, so a client left open leaks a pool
        per trading day (M16 final review, finding 9)."""

        def close(self) -> None:
            calls.append("close")

    fake_client = _FakeClient()

    def fake_client_factory(*_a: Any, **_kw: Any) -> object:
        calls.append("FdnClient")
        return fake_client

    def fake_ingest(*_args: Any, client: Any = None, **_kwargs: Any) -> int:
        calls.append("ingest")
        assert client is fake_client
        return 3

    def fake_ingest_events(*_args: Any, **_kwargs: Any) -> tuple[int, list[str]]:
        calls.append("ingest_events")
        return 2, ["calendar.economic"]

    def fake_fetch_news(*_args: Any, **_kwargs: Any) -> dict[str, list[str]]:
        calls.append("fetch_news")
        return {"ZHELD": ["ZHELD lands a contract"]}

    def fake_store_payloads(*_args: Any, **_kwargs: Any) -> int:
        calls.append("store_payloads")
        return 0

    captured_news: dict[str, list[str]] = {}
    captured_missing: list[str] = []

    def fake_assemble(*_args: Any, **kwargs: Any) -> object:
        calls.append("assemble")
        captured_news.update(kwargs.get("news") or {})
        captured_missing.extend(kwargs.get("missing") or [])
        raise _Stop

    monkeypatch.setattr("worker.providers.fdn.FdnClient", fake_client_factory)
    monkeypatch.setattr("worker.events_fdn.ingest_events_for_session", fake_ingest_events)
    monkeypatch.setattr("worker.news_fdn.fetch_held_news", fake_fetch_news)
    monkeypatch.setattr("worker.providers.fdn.store_captured_payloads", fake_store_payloads)
    monkeypatch.setattr(scheduler, "ingest_premarket_for_session", fake_ingest)
    monkeypatch.setattr("worker.assemble_open.assemble_open_and_store", fake_assemble)

    with pytest.raises(_Stop):
        scheduler.run_open_session_job(
            _engine(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 8, 13, 12, 15, tzinfo=UTC),
            healthcheck_url="",
        )
    assert calls == [
        "FdnClient", "ingest", "ingest_events", "fetch_news",
        "store_payloads", "close", "assemble",
    ]
    assert captured_news == {"ZHELD": ["ZHELD lands a contract"]}
    # Fix A (M16 final review): a partial calendar fetch must reach
    # `assemble_open_and_store` as `missing`, not just get swallowed at the
    # ingest call — this is the scheduler-level half of that wiring.
    assert captured_missing == ["calendar.economic"]
