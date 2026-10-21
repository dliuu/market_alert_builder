"""Task 8 (M22): the close job pulls options between the bar poll and assemble.

The stage mirrors ``_pull_close_catalysts_and_news``: a feed or database
failure costs the premium section, never the close brief. These tests drive it
without a network or a database, reusing ``test_scheduler``'s fakes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.engine import Connection

from tests.test_scheduler import _FakeEngine, _stub_poll, _TxEngine
from worker import config, options_ingest, scheduler
from worker.options_ingest import PullResult
from worker.providers.synthetic import SyntheticOptionsProvider

_SESSION = date(2026, 9, 4)
_CLOSE_FIRE = datetime(2026, 9, 4, 20, 45, tzinfo=UTC)  # 16:45 ET


def _boom(*a: object, **k: object) -> PullResult:
    raise RuntimeError("cboe down")


def _stub_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "held_symbols", lambda conn, user_id: {"ASTS", "SYM"})
    monkeypatch.setattr(
        options_ingest,
        "options_provider",
        lambda conn, symbols, session_date: SyntheticOptionsProvider({}, session_date),
    )


def test_a_raising_pull_returns_none_and_logs_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_held(monkeypatch)
    monkeypatch.setattr(options_ingest, "pull_options", _boom)

    assert (
        scheduler._pull_close_options(
            _FakeEngine(),  # type: ignore[arg-type]
            "u",
            _SESSION,
        )
        is None
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "options pull degraded" in out[0] and "cboe down" in out[0]


def test_a_successful_pull_prints_the_ok_symbols(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_held(monkeypatch)
    seen: dict[str, Any] = {}
    result = PullResult(
        symbols_ok=("ASTS",),
        symbols_failed={"SYM": "HTTPStatusError"},
        indices_ok=("VIX",),
        indices_failed={},
        raw_rows=3,
    )

    def fake_pull(conn: object, provider: object, symbols: list[str], d: date) -> PullResult:
        seen["symbols"] = symbols
        return result

    monkeypatch.setattr(options_ingest, "pull_options", fake_pull)

    assert (
        scheduler._pull_close_options(
            _FakeEngine(),  # type: ignore[arg-type]
            "u",
            _SESSION,
        )
        == result
    )
    assert seen["symbols"] == ["ASTS", "SYM"]
    out = capsys.readouterr().out
    assert f"close {_SESSION}: options" in out
    assert "['ASTS']" in out and "['VIX']" in out and "SYM" in out


def test_the_live_client_is_closed_even_when_the_pull_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker.providers import cboe

    closed: list[bool] = []

    class _Client(cboe.CboeClient):
        def __init__(self) -> None: ...

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(scheduler, "held_symbols", lambda conn, user_id: {"ASTS"})
    monkeypatch.setattr(config, "OPTIONS_LIVE", True)
    monkeypatch.setattr(cboe, "CboeClient", _Client)
    monkeypatch.setattr(options_ingest, "CboeClient", _Client)
    monkeypatch.setattr(options_ingest, "pull_options", _boom)

    assert (
        scheduler._pull_close_options(
            _FakeEngine(),  # type: ignore[arg-type]
            "u",
            _SESSION,
        )
        is None
    )
    assert closed == [True]


def test_the_synthetic_provider_is_priced_off_the_session_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worker import assemble

    monkeypatch.setattr(config, "OPTIONS_LIVE", False)
    monkeypatch.setattr(
        assemble,
        "_read_closes",
        lambda conn, symbols, d: {s: Decimal("10") for s in symbols},
    )
    provider = options_ingest.options_provider(None, ["ASTS"], _SESSION)  # type: ignore[arg-type]
    assert isinstance(provider, SyntheticOptionsProvider)
    assert provider.chain("ASTS")["data"]["symbol"] == "ASTS"


@pytest.mark.parametrize("quiet", [True, False])
def test_the_close_job_survives_a_raising_options_stage(
    monkeypatch: pytest.MonkeyPatch, quiet: bool
) -> None:
    """Review Focus 3, the job half: the options stage blowing up costs the
    premium section, never the send."""
    monkeypatch.setattr(scheduler, "ping_success", lambda url: None)
    monkeypatch.setattr(scheduler, "ping_fail", lambda url, d: None)
    _stub_poll(monkeypatch, missing=set())
    monkeypatch.setattr(scheduler, "_pull_close_catalysts_and_news", lambda e, u, d: {})
    # Pinned to the synthetic path, whatever the developer's .env says.
    monkeypatch.setattr(config, "OPTIONS_LIVE", False)
    _stub_held(monkeypatch)
    monkeypatch.setattr(options_ingest, "pull_options", _boom)

    from worker import assemble, deliver, narrate

    order: list[str] = []
    real = scheduler._pull_close_options

    def spy(engine: Any, user_id: str, session_date: date) -> PullResult | None:
        order.append("options")
        return real(engine, user_id, session_date)

    monkeypatch.setattr(scheduler, "_pull_close_options", spy)
    monkeypatch.setattr(narrate, "default_narrator", lambda: None)

    def fake_assemble(*a: object, **k: object) -> object | None:
        order.append("assemble")
        return None if quiet else object()

    class _Sent:
        status = "sent"
        provider_msg_id = "m1"

    monkeypatch.setattr(assemble, "assemble_and_store", fake_assemble)
    monkeypatch.setattr(deliver, "deliver_brief", lambda conn, **k: _Sent())

    outcome = scheduler.run_session_job(
        _TxEngine(),  # type: ignore[arg-type]
        now_utc=_CLOSE_FIRE,
        healthcheck_url="",
        recipient="me@example.invalid",
        sender="brief@example.invalid",
    )
    assert outcome == ("skipped-quiet" if quiet else "sent")
    assert order == ["options", "assemble"]


def test_a_book_with_no_snapshots_reads_empty_without_the_calendar(db_conn: Connection) -> None:
    """Assemble now always calls the volatility stage; a book whose options were
    never pulled must read as empty, even on a date past the calendar's bounds
    (the DB suites' 2099 fixtures)."""
    from worker.volatility import compute_and_store_volatility

    assert compute_and_store_volatility(db_conn, "u", ["ZZNOOPT"], date(2099, 1, 5)) == {}
