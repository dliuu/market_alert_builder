"""Scheduler + dead-man's switch (M10). Pure fire-time math, the poll loop, and
the go/no-go job's holiday-skip and failure-ping paths — all without a clock,
a network, or a database."""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from worker import calendar, scheduler

UTC = ZoneInfo("UTC")
_DELAY = timedelta(minutes=45)


# --- fire_time / next_fire (pure) -------------------------------------------


def test_fire_time_is_close_plus_delay() -> None:
    ft = scheduler.fire_time(date(2026, 9, 4), _DELAY)
    assert ft == datetime(2026, 9, 4, 20, 45, tzinfo=UTC)  # 16:45 ET


def test_fire_time_tracks_halfday() -> None:
    # Half-day closes 13:00 ET → fire at 13:45 ET == 18:45 UTC, not 20:45.
    ft = scheduler.fire_time(date(2026, 11, 27), _DELAY)
    assert ft == datetime(2026, 11, 27, 18, 45, tzinfo=UTC)


def test_next_fire_rolls_past_a_just_passed_time() -> None:
    # Just after Friday's fire (20:45 UTC) → next is Saturday's heartbeat.
    now = datetime(2026, 9, 4, 21, 0, tzinfo=UTC)
    nxt = scheduler.next_fire(now, _DELAY)
    assert nxt == datetime(2026, 9, 5, 20, 45, tzinfo=UTC)


def test_next_fire_returns_today_when_still_ahead() -> None:
    # Early Friday, before the fire time → today's fire.
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    assert scheduler.next_fire(now, _DELAY) == datetime(2026, 9, 4, 20, 45, tzinfo=UTC)


def test_next_fire_lands_on_holiday_as_heartbeat() -> None:
    # After Sunday's heartbeat → Labor Day Monday still gets a fire (the run will
    # skip the send, but the dead-man's switch must still check in).
    now = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    assert scheduler.next_fire(now, _DELAY) == datetime(2026, 9, 7, 20, 45, tzinfo=UTC)


# --- dead-man's switch pings ------------------------------------------------


def test_ping_success_hits_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(httpx, "get", lambda url, **k: seen.update(url=url))
    scheduler.ping_success("https://hc.example/abc")
    assert seen["url"] == "https://hc.example/abc"


def test_ping_fail_hits_fail_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(httpx, "post", lambda url, **k: seen.update(url=url))
    scheduler.ping_fail("https://hc.example/abc", "boom")
    assert seen["url"] == "https://hc.example/abc/fail"


def test_ping_empty_url_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("must not touch the network for an empty URL")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)
    scheduler.ping_success("")
    scheduler.ping_fail("", "x")


def test_ping_swallows_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _down(*a: Any, **k: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    monkeypatch.setattr(httpx, "post", _down)
    # A monitoring outage must never take down the send — both return quietly.
    scheduler.ping_success("https://hc.example/abc")
    scheduler.ping_fail("https://hc.example/abc", "x")


# --- ensure_todays_bars poll ------------------------------------------------


class _FakeEngine:
    def begin(self) -> Any:
        return contextlib.nullcontext(None)

    def connect(self) -> Any:
        return contextlib.nullcontext(None)


def test_ensure_todays_bars_polls_until_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import ingest, normalize

    monkeypatch.setattr(ingest, "ingest_daily_bars", lambda *a, **k: 0)
    monkeypatch.setattr(normalize, "normalize_bars", lambda *a, **k: 0)

    # The bar shows up on the third check; the first two are misses.
    checks = {"n": 0}

    def _present(_engine: Any, symbols: list[str], _d: date) -> set[str]:
        checks["n"] += 1
        return set(symbols) if checks["n"] >= 3 else set()

    monkeypatch.setattr(scheduler, "_bars_present", _present)

    slept: list[float] = []
    clock = {"t": 0.0}

    def _mono() -> float:
        return clock["t"]

    def _sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    missing = scheduler.ensure_todays_bars(
        _FakeEngine(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        symbols=["SPY", "ASTS"],
        session_date=date(2026, 9, 4),
        timeout_s=1000,
        interval_s=90,
        now_monotonic=_mono,
        sleep=_sleep,
    )
    assert missing == set()
    assert slept == [90, 90]  # slept between the three checks, not after success


def test_ensure_todays_bars_gives_up_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from worker import ingest, normalize

    monkeypatch.setattr(ingest, "ingest_daily_bars", lambda *a, **k: 0)
    monkeypatch.setattr(normalize, "normalize_bars", lambda *a, **k: 0)
    monkeypatch.setattr(scheduler, "_bars_present", lambda *a, **k: {"SPY"})  # ASTS never lands

    clock = {"t": 0.0}

    def _mono() -> float:
        return clock["t"]

    def _sleep(s: float) -> None:
        clock["t"] += s

    missing = scheduler.ensure_todays_bars(
        _FakeEngine(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        symbols=["SPY", "ASTS"],
        session_date=date(2026, 9, 4),
        timeout_s=200,
        interval_s=90,
        now_monotonic=_mono,
        sleep=_sleep,
    )
    assert missing == {"ASTS"}  # returns what's still missing rather than hanging


# --- run_session_job go/no-go paths -----------------------------------------


def test_run_session_job_skips_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    # Labor Day 16:45 ET == 2026-09-07 20:45 UTC. No engine access on this path.
    outcome = scheduler.run_session_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 7, 20, 45, tzinfo=UTC),
        healthcheck_url="https://hc.example/abc",
    )
    assert outcome == "skipped-holiday"
    assert pings == [("ok", "https://hc.example/abc")]  # success ping, no /fail


def test_run_session_job_pings_fail_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    def _boom(_d: date) -> bool:
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(calendar, "is_session", _boom)

    with pytest.raises(RuntimeError, match="calendar exploded"):
        scheduler.run_session_job(
            object(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 9, 4, 20, 45, tzinfo=UTC),
            healthcheck_url="https://hc.example/abc",
        )
    assert pings == [("fail", "https://hc.example/abc")]  # /fail pinged, then raised
