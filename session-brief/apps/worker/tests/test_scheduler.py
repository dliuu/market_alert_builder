"""Scheduler + dead-man's switch (M10). Pure fire-time math, the poll loop, and
the go/no-go job's holiday-skip and failure-ping paths — mostly without a clock,
a network, or a database; one DB test at the bottom drives run_reconcile_job's
real body end to end (skipped without DATABASE_URL)."""

from __future__ import annotations

import contextlib
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from tests.helpers_attribution import seed_bars_for
from worker import calendar, scheduler
from worker.attribution import refit, score
from worker.constants import ATTRIBUTION_MODEL_VERSION as MV
from worker.themes_seed import seed_themes

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


# --- next_session_fire / next_weekly_fire (pure) ----------------------------


def test_next_weekly_fire_hits_next_saturday_0800_et() -> None:
    # Fri 2026-09-04 12:00 UTC → Sat 2026-09-05 08:00 ET == 12:00 UTC.
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    ft = scheduler.next_weekly_fire(now, weekday=5, at_et=time(8, 0))
    assert ft == datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def test_next_session_fire_pm_1830_et_skips_holiday() -> None:
    # Sat 2026-09-05 → next session is Tue 2026-09-08 (Mon is Labor Day);
    # 18:30 ET == 22:30 UTC.
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    ft = scheduler.next_session_fire(now, at_et=time(18, 30))
    assert ft == datetime(2026, 9, 8, 22, 30, tzinfo=UTC)


def test_next_session_fire_rolls_when_time_passed() -> None:
    # Fri 2026-09-04 23:00 UTC is past 18:30 ET (22:30 UTC) → next session Tue.
    now = datetime(2026, 9, 4, 23, 0, tzinfo=UTC)
    ft = scheduler.next_session_fire(now, at_et=time(18, 30))
    assert ft == datetime(2026, 9, 8, 22, 30, tzinfo=UTC)


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


def test_ping_log_hits_log_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # /log records an event *without* moving the check's status — the right ping
    # for "not failed, not finished either" (a deferral with a retry pending).
    seen: dict[str, Any] = {}
    monkeypatch.setattr(httpx, "post", lambda url, **k: seen.update(url=url))
    scheduler.ping_log("https://hc.example/abc", "waiting on bars")
    assert seen["url"] == "https://hc.example/abc/log"


def test_ping_empty_url_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("must not touch the network for an empty URL")

    monkeypatch.setattr(httpx, "get", _boom)
    monkeypatch.setattr(httpx, "post", _boom)
    scheduler.ping_success("")
    scheduler.ping_fail("", "x")
    scheduler.ping_log("", "x")


def test_ping_swallows_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _down(*a: Any, **k: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    monkeypatch.setattr(httpx, "post", _down)
    # A monitoring outage must never take down the send — all three return quietly.
    scheduler.ping_success("https://hc.example/abc")
    scheduler.ping_fail("https://hc.example/abc", "x")
    scheduler.ping_log("https://hc.example/abc", "x")


# --- retry_fire_time (pure) --------------------------------------------------


def test_retry_fire_time_is_now_plus_interval() -> None:
    # Gave up at 17:05 ET (21:05 UTC) → retry 30 minutes later, same session.
    now = datetime(2026, 9, 4, 21, 5, tzinfo=UTC)
    nxt = scheduler.retry_fire_time(now, date(2026, 9, 4), interval=timedelta(minutes=30))
    assert nxt == datetime(2026, 9, 4, 21, 35, tzinfo=UTC)


def test_retry_fire_time_stops_at_the_deadline() -> None:
    # 23:40 ET is inside the day but now+30min (00:10 ET) is past the 23:45 ET
    # cutoff — no retry rather than one that lands tomorrow.
    now = datetime(2026, 9, 5, 3, 40, tzinfo=UTC)  # 2026-09-04 23:40 ET
    nxt = scheduler.retry_fire_time(now, date(2026, 9, 4), interval=timedelta(minutes=30))
    assert nxt is None


def test_retry_fire_time_never_crosses_midnight_et() -> None:
    """The deadline exists to protect ``session_date``.

    ``run_session_job`` derives the session from ``calendar.today_et(now)``, so a
    retry that fired after midnight ET would silently compute the *next* day's
    session against today's book. Every retry must land on the session date it
    was scheduled for.
    """
    session = date(2026, 9, 4)
    now = datetime(2026, 9, 4, 21, 5, tzinfo=UTC)
    for _ in range(200):
        nxt = scheduler.retry_fire_time(now, session, interval=timedelta(minutes=30))
        if nxt is None:
            break
        assert calendar.today_et(nxt) == session
        now = nxt
    else:  # pragma: no cover - a runaway retry chain is the bug under test
        raise AssertionError("retry chain never terminated")


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


def test_ensure_todays_bars_gates_on_required_not_everything_ingested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late theme member must not hold up the send.

    The poll ingests the whole universe — held names, benchmarks, and the
    attribution model's theme constituents — but only the held names and SPY are
    load-bearing for tonight's numbers. Reporting a missing theme peer as
    "missing" would defer a brief that is fully computable.
    """
    from worker import ingest, normalize

    ingested: list[list[str]] = []

    def _record_ingest(conn: Any, prov: Any, syms: list[str], *a: Any, **k: Any) -> int:
        ingested.append(list(syms))
        return 0

    monkeypatch.setattr(ingest, "ingest_daily_bars", _record_ingest)
    monkeypatch.setattr(normalize, "normalize_bars", lambda *a, **k: 0)
    # ZPEER never lands; SPY and the held name do.
    monkeypatch.setattr(scheduler, "_bars_present", lambda *a, **k: {"SPY", "HELD"})

    missing = scheduler.ensure_todays_bars(
        _FakeEngine(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        symbols=["SPY", "HELD", "ZPEER"],
        session_date=date(2026, 9, 4),
        required=["SPY", "HELD"],
        timeout_s=0,
        interval_s=1,
        now_monotonic=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert missing == set()                       # the send is not blocked
    assert ingested[0] == ["SPY", "HELD", "ZPEER"]  # ...but ZPEER was still fetched


def test_ensure_todays_bars_defaults_required_to_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting `required` keeps the old behaviour: gate on all of `symbols`."""
    from worker import ingest, normalize

    monkeypatch.setattr(ingest, "ingest_daily_bars", lambda *a, **k: 0)
    monkeypatch.setattr(normalize, "normalize_bars", lambda *a, **k: 0)
    monkeypatch.setattr(scheduler, "_bars_present", lambda *a, **k: {"SPY"})

    missing = scheduler.ensure_todays_bars(
        _FakeEngine(),  # type: ignore[arg-type]
        provider=object(),  # type: ignore[arg-type]
        symbols=["SPY", "HELD"],
        session_date=date(2026, 9, 4),
        timeout_s=0,
        interval_s=1,
        now_monotonic=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert missing == {"HELD"}
# --- next_after_outcome: where the one-shot points next (pure) ---------------


def test_next_after_outcome_schedules_a_retry_on_deferral() -> None:
    # Friday 17:05 ET, deferred. Next fire is the retry, not Saturday's heartbeat.
    now = datetime(2026, 9, 4, 21, 5, tzinfo=UTC)
    nxt, kind = scheduler.next_after_outcome(
        now, "deferred-no-bars", session_date=date(2026, 9, 4), delay=_DELAY
    )
    assert kind == "close"
    assert nxt == datetime(2026, 9, 4, 21, 35, tzinfo=UTC)


def test_next_after_outcome_falls_back_to_normal_schedule_past_deadline() -> None:
    # 23:50 ET — no retry left, so resume the ordinary rotation.
    now = datetime(2026, 9, 5, 3, 50, tzinfo=UTC)
    nxt, kind = scheduler.next_after_outcome(
        now, "failed-no-bars", session_date=date(2026, 9, 4), delay=_DELAY
    )
    assert (nxt, kind) == scheduler.next_kind_fire(now, _DELAY)


def test_next_after_outcome_leaves_a_sent_run_on_the_normal_schedule() -> None:
    now = datetime(2026, 9, 4, 21, 5, tzinfo=UTC)
    nxt, kind = scheduler.next_after_outcome(
        now, "sent", session_date=date(2026, 9, 4), delay=_DELAY
    )
    assert (nxt, kind) == scheduler.next_kind_fire(now, _DELAY)


def test_retry_never_displaces_the_next_open_brief() -> None:
    """One job id means a retry scheduled past the next fire would *drop* it.

    The deadline already keeps retries same-day, so this is a guard rather than a
    live path — but the failure it prevents is losing a morning brief entirely.
    """
    now = datetime(2026, 9, 4, 21, 5, tzinfo=UTC)
    nxt_regular, _ = scheduler.next_kind_fire(now, _DELAY)
    nxt, _kind = scheduler.next_after_outcome(
        now, "deferred-no-bars", session_date=date(2026, 9, 4), delay=_DELAY
    )
    assert nxt <= nxt_regular


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


def _stub_poll(monkeypatch: pytest.MonkeyPatch, missing: set[str]) -> None:
    """Drive run_session_job to the poll's verdict without a DB or a network."""
    monkeypatch.setattr(scheduler, "ingest_symbols", lambda conn, user_id: ["SPY", "ASTS"])
    monkeypatch.setattr(scheduler, "required_symbols", lambda conn, user_id: ["SPY", "ASTS"])
    monkeypatch.setattr(scheduler, "_default_provider", lambda: object())
    monkeypatch.setattr(scheduler, "ensure_todays_bars", lambda *a, **k: missing)


def test_run_session_job_defers_when_bars_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: a late vendor must defer, not crash.

    Before this, a give-up printed a warning and fell through to compute, which
    raised ``no bars for … run backfill`` — turning a known, diagnosed "the data
    isn't published yet" into an unhandled exception and a red check.
    """
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    monkeypatch.setattr(scheduler, "ping_log", lambda url, d: pings.append(("log", url)))
    _stub_poll(monkeypatch, missing={"ASTS"})

    from worker import assemble

    def _must_not_assemble(*a: Any, **k: Any) -> None:
        raise AssertionError("assemble must not run when today's bars are missing")

    monkeypatch.setattr(assemble, "assemble_and_store", _must_not_assemble)

    outcome = scheduler.run_session_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 20, 45, tzinfo=UTC),
        healthcheck_url="https://hc.example/abc",
    )
    assert outcome == "deferred-no-bars"
    # /log, not /fail: the day isn't over, a retry is pending.
    assert pings == [("log", "https://hc.example/abc")]


def test_run_session_job_deadline_exhaustion_pings_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the retry deadline the day has genuinely failed — go red."""
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    monkeypatch.setattr(scheduler, "ping_log", lambda url, d: pings.append(("log", url)))
    _stub_poll(monkeypatch, missing={"ASTS"})

    outcome = scheduler.run_session_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 5, 3, 50, tzinfo=UTC),  # 23:50 ET — past the cutoff
        healthcheck_url="https://hc.example/abc",
    )
    assert outcome == "failed-no-bars"
    assert pings == [("fail", "https://hc.example/abc")]


def test_run_session_job_proceeds_when_bars_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path is untouched: an empty `missing` still runs the pipeline."""
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    monkeypatch.setattr(scheduler, "ping_log", lambda url, d: pings.append(("log", url)))
    _stub_poll(monkeypatch, missing=set())

    from worker import assemble, narrate

    monkeypatch.setattr(narrate, "default_narrator", lambda: None)
    monkeypatch.setattr(assemble, "assemble_and_store", lambda *a, **k: None)  # quiet session

    class _Tx:
        def commit(self) -> None: ...
        def rollback(self) -> None: ...

    class _TxEngine(_FakeEngine):
        """connect() must yield a connection whose begin() is a real transaction."""

        def connect(self) -> Any:
            class _Conn:
                def begin(self) -> Any:
                    return _Tx()

            return contextlib.nullcontext(_Conn())

    outcome = scheduler.run_session_job(
        _TxEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 20, 45, tzinfo=UTC),
        healthcheck_url="https://hc.example/abc",
    )
    assert outcome == "skipped-quiet"
    assert pings == [("ok", "https://hc.example/abc")]


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


# --- run_refit_job / run_pm_score_job / run_reconcile_job -------------------


def test_run_refit_job_pings_success(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    from worker import attribution, maintenance

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        attribution, "refit", lambda conn, fit_date, **k: seen.update(fit_date=fit_date)
    )
    monkeypatch.setattr(
        maintenance, "surface_maintenance_flags", lambda conn, user_id, session_date, mv: []
    )
    out = scheduler.run_refit_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),  # Saturday
        healthcheck_url="https://hc.example/refit",
    )
    assert out == "refit"
    assert seen["fit_date"] == date(2026, 9, 4)  # last session before Saturday
    assert pings == [("ok", "https://hc.example/refit")]


def test_run_refit_job_surfaces_maintenance_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    from worker import attribution, maintenance
    from worker.constants import DEV_USER_ID

    monkeypatch.setattr(attribution, "refit", lambda conn, fit_date, **k: None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        maintenance,
        "surface_maintenance_flags",
        lambda conn, user_id, session_date, model_version: seen.update(
            user_id=user_id, session_date=session_date, model_version=model_version
        )
        or [],
    )
    out = scheduler.run_refit_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),  # Saturday
        healthcheck_url="https://hc.example/refit",
    )
    assert out == "refit"
    assert seen["user_id"] == DEV_USER_ID
    assert seen["session_date"] == date(2026, 9, 4)  # last session before Saturday
    assert pings == [("ok", "https://hc.example/refit")]


def test_run_pm_score_job_scores_today_synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    from worker import attribution

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        attribution,
        "score",
        lambda conn, trade_date, **k: seen.update(trade_date=trade_date, **k),
    )
    out = scheduler.run_pm_score_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 22, 30, tzinfo=UTC),  # Friday, a session day
        healthcheck_url="https://hc.example/pm",
    )
    assert out == "pm-scored"
    assert seen["trade_date"] == date(2026, 9, 4)
    assert seen["synthetic"] is True
    assert pings == [("ok", "https://hc.example/pm")]


def test_run_pm_score_job_skips_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    # Labor Day — no session; scoring must not run at all.
    out = scheduler.run_pm_score_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 7, 22, 30, tzinfo=UTC),
        healthcheck_url="https://hc.example/pm",
    )
    assert out == "skipped-holiday"
    assert pings == [("ok", "https://hc.example/pm")]


def test_run_reconcile_job_reconciles_prev_session(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    from worker import reconcile as recon_mod

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        recon_mod,
        "reconcile",
        lambda conn, trade_date, **k: seen.update(trade_date=trade_date),
    )
    # Tue 2026-09-08 07:00 ET; prior session is Fri 2026-09-04 (Mon holiday).
    out = scheduler.run_reconcile_job(
        _FakeEngine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 8, 11, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/am",
    )
    assert out == "reconciled"
    assert seen["trade_date"] == date(2026, 9, 4)
    assert pings == [("ok", "https://hc.example/am")]


def test_run_reconcile_job_skips_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    # Labor Day — no session; reconcile must not run at all.
    out = scheduler.run_reconcile_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 7, 11, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/am",
    )
    assert out == "skipped-holiday"
    assert pings == [("ok", "https://hc.example/am")]


def test_run_refit_job_pings_fail_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    from worker import attribution

    def _boom(conn: Any, fit_date: date, **k: Any) -> None:
        raise RuntimeError("refit exploded")

    monkeypatch.setattr(attribution, "refit", _boom)

    with pytest.raises(RuntimeError, match="refit exploded"):
        scheduler.run_refit_job(
            _FakeEngine(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            healthcheck_url="https://hc.example/refit",
        )
    assert pings == [("fail", "https://hc.example/refit")]


# --- run_reconcile_job through the real scheduled path (DB, DoD #4) ---------
#
# The tests above monkeypatch worker.reconcile.reconcile, so the join between
# the scheduler's calendar math (today_et -> previous_session) and the real
# reconcile() body is untested. That path needs a genuine DB.
#
# run_reconcile_job takes an Engine and opens its own `engine.begin()`. The
# project's only DB fixture (`db_conn`) is a single connection whose
# transaction is rolled back at teardown — the safe pattern every other DB
# test in this repo relies on, including the reconcile seeding this test
# mirrors (tests/test_reconcile.py), which seeds real symbols (SPY, NVDA, ...)
# into shared, unscoped tables (bars_daily, attribution_fits, basket_returns).
# Handing run_reconcile_job a *real* Engine would make it open a second, truly
# committed transaction — risking a permanent overwrite of real historical
# bars for those symbols if this dev DB already has them backfilled, with no
# way to undo it afterwards. `_ConnEngine` avoids that: it satisfies the same
# `.begin()` duck type run_reconcile_job expects (the pattern `_FakeEngine`
# above already uses for the connectionless tests) but hands back the
# fixture's own open connection instead of a second real transaction, so every
# write here still rolls back at fixture teardown like any other DB test.


class _ConnEngine:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def begin(self) -> Any:
        return contextlib.nullcontext(self._conn)


def test_run_reconcile_job_flips_revised_through_scheduled_path(db_conn: Connection) -> None:
    # Mirrors tests/test_reconcile.py's seeding: fit + PM-synthesize a session,
    # then corrupt one symbol's synthetic total_bps so it diverges from the
    # real official bar beyond RECONCILE_TOL.
    symbols = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "SPY"]
    trade_date = date(2020, 6, 30)
    fit_now = datetime(2020, 6, 30, 22, 0, tzinfo=UTC)

    seed_themes(db_conn)
    seed_bars_for(db_conn, symbols, sessions=121, end=trade_date)
    refit(db_conn, trade_date, now_utc=fit_now, model_version=MV)
    score(db_conn, trade_date, now_utc=fit_now, model_version=MV, synthetic=True)
    db_conn.execute(
        text(
            "UPDATE attribution SET total_bps = 99999 "
            "WHERE trade_date = :d AND symbol = 'NVDA' AND model_version = :mv"
        ),
        {"d": trade_date, "mv": MV},
    )

    # Wed 2020-07-01, 07:00 ET (a real session) -> previous_session == trade_date.
    reconcile_now = datetime(2020, 7, 1, 11, 0, tzinfo=UTC)
    assert calendar.today_et(reconcile_now) == date(2020, 7, 1)
    assert calendar.previous_session(date(2020, 7, 1)) == trade_date

    outcome = scheduler.run_reconcile_job(
        _ConnEngine(db_conn),  # type: ignore[arg-type]
        now_utc=reconcile_now,
        healthcheck_url="",
    )
    assert outcome == "reconciled"

    row = db_conn.execute(
        text(
            "SELECT revised, provisional, synthetic FROM attribution "
            "WHERE trade_date = :d AND symbol = 'NVDA' AND model_version = :mv"
        ),
        {"d": trade_date, "mv": MV},
    ).mappings().one()
    # The divergent bar flips revised end to end, through run_reconcile_job's
    # own calendar math and engine.begin() — not just reconcile() in isolation.
    assert row["revised"] is True
    assert row["provisional"] is False
    assert row["synthetic"] is False
