"""The CN scheduler (CN-M2): the 09:10 CST open fire, the CN close heartbeat,
and their join into the shared four-kind ``next_kind_fire``.

Pure — no clock, no network, no DB, mirroring ``tests/test_scheduler_open.py``'s
style. Real 2026 dates: Fri 2026-09-04 is both an XNYS and an XSHG session;
Thu 2026-10-01 is Golden Week (XSHG holiday, XNYS session — U.S. markets don't
observe it); Mon 2026-09-07 is US Labor Day but an ordinary XSHG session —
every instant below was checked against ``exchange_calendars`` (via
``worker_cn.calendar.CN`` / ``worker.calendar.US``) before being pinned here.

Core design rule under test (controller ruling, binding): each kind's next
fire is computed independently in its own market's calendar — a shared
"today" is wrong across markets, since Shanghai's Monday 09:10 is Sunday
evening ET.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from worker import config
from worker import scheduler as us_scheduler
from worker_cn import config as cn_config
from worker_cn import scheduler as cn_scheduler
from worker_cn.constants import CN_BENCHMARK

UTC = ZoneInfo("UTC")
_DELAY = timedelta(minutes=45)


# --- cn_open_fire_time / cn_close_fire_time (pure) ---------------------------


def test_cn_open_fire_is_0910_cst() -> None:
    # 09:10 CST == 01:10 UTC (Asia/Shanghai has no DST).
    assert cn_scheduler.cn_open_fire_time(date(2026, 9, 4)) == datetime(
        2026, 9, 4, 1, 10, tzinfo=UTC
    )


def test_cn_close_fire_is_1520_cst() -> None:
    # Session close 15:00 CST + 20min delay == 15:20 CST == 07:20 UTC.
    assert cn_scheduler.cn_close_fire_time(date(2026, 9, 4)) == datetime(
        2026, 9, 4, 7, 20, tzinfo=UTC
    )


def test_golden_week_has_no_open_fire_but_keeps_the_close_heartbeat() -> None:
    """The CN open brief has nothing to say on a holiday (no session to read
    the tape against) — ``None``, not a nominal instant. The close heartbeat
    still fires (the CN dead-man's switch, mirroring the US close's D20
    semantics), and neither US kind is affected: 2026-10-01 is an ordinary
    XNYS session, so US open/close land at their normal wall-clock times."""
    golden_week = date(2026, 10, 1)

    assert cn_scheduler.cn_open_fire_time(golden_week) is None
    assert cn_scheduler.cn_close_fire_time(golden_week) == datetime(
        2026, 10, 1, 7, 20, tzinfo=UTC
    )

    # The US kinds are unaffected by CN's holiday.
    assert us_scheduler.open_fire_time(golden_week) == datetime(2026, 10, 1, 12, 15, tzinfo=UTC)
    assert us_scheduler.fire_time(golden_week, _DELAY) == datetime(
        2026, 10, 1, 20, 45, tzinfo=UTC
    )


# --- next_cn_open_fire / next_cn_close_fire (pure) ----------------------------


def test_next_cn_open_fire_skips_golden_week() -> None:
    # Golden Week 2026-10-01..07 (verified in tests/cn/test_calendar_cn.py); the
    # first session back is 2026-10-08.
    now = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)  # after 9/30's own open fire
    nxt = cn_scheduler.next_cn_open_fire(now)
    assert nxt == datetime(2026, 10, 8, 1, 10, tzinfo=UTC)


def test_next_cn_close_fire_is_a_daily_heartbeat_through_golden_week() -> None:
    # Unlike the open fire, the close heartbeat doesn't skip Golden Week days.
    now = datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    nxt = cn_scheduler.next_cn_close_fire(now)
    assert nxt == datetime(2026, 10, 1, 7, 20, tzinfo=UTC)


# --- next_kind_fire: the four-way min, per-market (pure) ---------------------


def test_date_disagreement_next_fire_is_mondays_cn_open() -> None:
    """The DoD proof: Shanghai's Monday morning is Sunday evening ET. Sunday
    2026-09-06 20:00 ET (00:00 UTC Monday) — Monday 2026-09-07 is US Labor Day
    (no US candidate that day at all) but an ordinary XSHG session, so the very
    next fire of any kind is Monday's CN open, not a US one."""
    now = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)  # 20:00 ET Sunday 2026-09-06
    when, kind = us_scheduler.next_kind_fire(now, _DELAY)
    assert kind == "open_cn"
    assert when == datetime(2026, 9, 7, 1, 10, tzinfo=UTC)


def test_a_full_shared_session_day_yields_four_fires_in_order() -> None:
    """2026-09-04 is a session in both markets. Walked from just after the
    prior US close, the next four fires are exactly the four kinds, all dated
    2026-09-04, in CN-then-US order (Shanghai's morning precedes New York's)."""
    now = datetime(2026, 9, 3, 21, 0, tzinfo=UTC)  # just after Thursday's US close
    fires = []
    for _ in range(4):
        when, kind = us_scheduler.next_kind_fire(now, _DELAY)
        fires.append((when, kind))
        now = when + timedelta(seconds=1)

    assert [k for _, k in fires] == ["open_cn", "close_cn", "open", "close"]
    assert all(w.date() == date(2026, 9, 4) for w, _ in fires)
    assert fires == sorted(fires)  # strictly increasing


# --- _default_cn_provider ------------------------------------------------


def test_default_cn_provider_is_synthetic_when_bars_are_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker_cn.providers import SyntheticCnBarsProvider

    monkeypatch.setattr(cn_config, "CN_BARS_LIVE", False)
    assert isinstance(cn_scheduler._default_cn_provider(), SyntheticCnBarsProvider)


def test_default_cn_provider_raises_when_live_is_flipped_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live provider lands in CN-M3 (Task 10); flipping CN_BARS_LIVE on
    # before then must fail loudly rather than silently fall back.
    monkeypatch.setattr(cn_config, "CN_BARS_LIVE", True)
    with pytest.raises(RuntimeError, match="CN-M3"):
        cn_scheduler._default_cn_provider()


# --- run_cn_open_session_job / run_cn_close_session_job -----------------------
#
# Minimal engine stand-ins mirroring tests/test_scheduler_open.py's
# _StubEngine/_Stop pattern — neither job needs a real database here, since
# every DB-touching collaborator is monkeypatched at its own module boundary.


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


def test_cn_close_job_skips_holiday_and_pings_only_its_own_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Golden Week: non-session -> success ping, no engine access. Mocks httpx
    directly (test_scheduler.py's ping-test pattern) with a decoy US URL set to
    prove the CN job never touches it."""
    monkeypatch.setattr(config, "HEALTHCHECKS_URL", "https://hc.example/us-close")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **k: seen.append(url))

    outcome = cn_scheduler.run_cn_close_session_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/cn-close",
    )
    assert outcome == "skipped-holiday"
    assert seen == ["https://hc.example/cn-close"]


def test_cn_open_job_skips_holiday_and_pings_only_its_own_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HEALTHCHECKS_OPEN_URL", "https://hc.example/us-open")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **k: seen.append(url))

    outcome = cn_scheduler.run_cn_open_session_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/cn-open",
    )
    assert outcome == "skipped-holiday"
    assert seen == ["https://hc.example/cn-open"]


def test_cn_close_job_resolves_its_own_env_url_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``healthcheck_url`` override -> resolves ``HEALTHCHECKS_CN_CLOSE_URL``,
    never the US ``HEALTHCHECKS_URL``."""
    monkeypatch.setattr(cn_config, "HEALTHCHECKS_CN_CLOSE_URL", "https://hc.example/cn-close-env")
    monkeypatch.setattr(config, "HEALTHCHECKS_URL", "https://hc.example/us-close")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **k: seen.append(url))

    outcome = cn_scheduler.run_cn_close_session_job(
        object(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 10, 1, 3, 0, tzinfo=UTC),
    )
    assert outcome == "skipped-holiday"
    assert seen == ["https://hc.example/cn-close-env"]


def test_run_cn_close_session_job_full_pipeline_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring order: book_symbols -> ensure_todays_bars (source-scoped) ->
    assemble_cn_close_and_store -> deliver_brief(kind="close_cn") -> success
    ping."""
    pings: list[Any] = []
    monkeypatch.setattr(us_scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(us_scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    calls: list[str] = []

    def fake_book_symbols(conn: Any, user_id: str, *, market: str, benchmark: str) -> list[str]:
        calls.append("book_symbols")
        assert market == "CN"
        assert benchmark == CN_BENCHMARK
        return ["000001.SZ"]

    def fake_ensure(
        engine: Any, provider: Any, symbols: list[str], session_date: Any, **kw: Any
    ) -> set[str]:
        calls.append("ensure_todays_bars")
        assert symbols == ["000001.SZ"]
        assert kw.get("source") == "synthetic-cn"
        return set()

    fake_obj = SimpleNamespace(brief_id="u-2026-09-04-close_cn")

    def fake_assemble(conn: Any, user_id: str, session_date: Any) -> Any:
        calls.append("assemble")
        return fake_obj

    def fake_deliver(conn: Any, **kw: Any) -> Any:
        calls.append("deliver")
        assert kw["kind"] == "close_cn"
        return SimpleNamespace(status="sent", provider_msg_id="m1")

    monkeypatch.setattr(us_scheduler, "book_symbols", fake_book_symbols)
    monkeypatch.setattr(us_scheduler, "ensure_todays_bars", fake_ensure)
    monkeypatch.setattr("worker_cn.assemble.assemble_cn_close_and_store", fake_assemble)
    monkeypatch.setattr("worker.deliver.deliver_brief", fake_deliver)

    outcome = cn_scheduler.run_cn_close_session_job(
        _engine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/cn-close",
    )
    assert outcome == "sent"
    assert calls == ["book_symbols", "ensure_todays_bars", "assemble", "deliver"]
    assert pings == [("ok", "https://hc.example/cn-close")]


def test_run_cn_close_session_job_quiet_session_skips_the_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(us_scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(us_scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    monkeypatch.setattr(us_scheduler, "book_symbols", lambda *a, **k: ["000001.SZ"])
    monkeypatch.setattr(us_scheduler, "ensure_todays_bars", lambda *a, **k: set())
    monkeypatch.setattr("worker_cn.assemble.assemble_cn_close_and_store", lambda *a, **k: None)

    def _must_not_deliver(*a: Any, **k: Any) -> None:
        raise AssertionError("a quiet session must not reach delivery")

    monkeypatch.setattr("worker.deliver.deliver_brief", _must_not_deliver)

    outcome = cn_scheduler.run_cn_close_session_job(
        _engine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
        healthcheck_url="https://hc.example/cn-close",
    )
    assert outcome == "skipped-quiet"
    assert pings == [("ok", "https://hc.example/cn-close")]


def test_run_cn_close_session_job_pings_fail_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(us_scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(us_scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))
    monkeypatch.setattr(us_scheduler, "book_symbols", lambda *a, **k: ["000001.SZ"])
    monkeypatch.setattr(us_scheduler, "ensure_todays_bars", lambda *a, **k: set())

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("no CN holdings for user")

    monkeypatch.setattr("worker_cn.assemble.assemble_cn_close_and_store", _boom)

    with pytest.raises(RuntimeError, match="no CN holdings"):
        cn_scheduler.run_cn_close_session_job(
            _engine(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
            healthcheck_url="https://hc.example/cn-close",
        )
    # /fail pinged, never /ok — no stale send.
    assert pings == [("fail", "https://hc.example/cn-close")]


def test_run_cn_open_session_job_full_pipeline_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring order: assemble_cn_open_and_store -> deliver_brief(kind="open_cn")
    -> success ping. No bar poll on this path (docs/05: the open brief always
    sends, no skip gate)."""
    pings: list[Any] = []
    monkeypatch.setattr(us_scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(us_scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    calls: list[str] = []
    fake_obj = SimpleNamespace(brief_id="u-2026-09-04-open_cn")

    def fake_assemble(conn: Any, user_id: str, session_date: Any, **kw: Any) -> Any:
        calls.append("assemble")
        assert kw["prior_session"] == date(2026, 9, 3)
        return fake_obj

    def fake_deliver(conn: Any, **kw: Any) -> Any:
        calls.append("deliver")
        assert kw["kind"] == "open_cn"
        return SimpleNamespace(status="sent", provider_msg_id="m1")

    monkeypatch.setattr("worker_cn.assemble.assemble_cn_open_and_store", fake_assemble)
    monkeypatch.setattr("worker.deliver.deliver_brief", fake_deliver)

    outcome = cn_scheduler.run_cn_open_session_job(
        _engine(),  # type: ignore[arg-type]
        now_utc=datetime(2026, 9, 4, 1, 10, tzinfo=UTC),
        healthcheck_url="https://hc.example/cn-open",
    )
    assert outcome == "sent"
    assert calls == ["assemble", "deliver"]
    assert pings == [("ok", "https://hc.example/cn-open")]


def test_run_cn_open_session_job_pings_fail_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pings: list[Any] = []
    monkeypatch.setattr(us_scheduler, "ping_success", lambda url: pings.append(("ok", url)))
    monkeypatch.setattr(us_scheduler, "ping_fail", lambda url, d: pings.append(("fail", url)))

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("worker_cn.assemble.assemble_cn_open_and_store", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        cn_scheduler.run_cn_open_session_job(
            _engine(),  # type: ignore[arg-type]
            now_utc=datetime(2026, 9, 4, 1, 10, tzinfo=UTC),
            healthcheck_url="https://hc.example/cn-open",
        )
    assert pings == [("fail", "https://hc.example/cn-open")]
