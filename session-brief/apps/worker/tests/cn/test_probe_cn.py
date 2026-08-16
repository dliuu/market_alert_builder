"""tiingo-cn-probe formatting logic (CN-M3 Task 10). Stubbed provider/client
only — no live network calls, per the repo-wide pytest rule."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from worker.providers.tiingo import TiingoProvider
from worker_cn.probe import DEFAULT_SYMBOLS, _candidates, tiingo_cn_probe

# A plain Friday XSHG session (matches tests/cn/test_providers_cn.py's pin).
_TODAY = date(2026, 8, 14)


def _bar(d: str, close: str, adj_close: str) -> dict[str, Any]:
    return {
        "date": f"{d}T00:00:00.000Z",
        "open": Decimal("1"),
        "high": Decimal("1"),
        "low": Decimal("1"),
        "close": Decimal(close),
        "adjClose": Decimal(adj_close),
        "volume": 100,
    }


def _not_found() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.tiingo.com/tiingo/daily/x/prices")
    return httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404))


def test_default_symbols() -> None:
    assert DEFAULT_SYMBOLS == ["600519.SS", "300750.SZ", "000300.SS"]


def test_candidates_for_shanghai_symbol() -> None:
    assert _candidates("600519.SS") == ["600519-SHG", "600519.SS", "600519", "600519-SS"]


def test_candidates_for_shenzhen_symbol() -> None:
    assert _candidates("300750.SZ") == ["300750-SHE", "300750.SZ", "300750", "300750-SZ"]


def test_probe_reports_first_working_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the `-SHG` candidate returns data; the probe must report it as the
    resolved format without trying the rest needlessly (though trying them is
    also fine — the assertion is on what gets reported, not call count)."""

    def fake_fetch(
        self: TiingoProvider, vendor_symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        if vendor_symbol == "600519-SHG":
            return [
                _bar("2026-08-13", "10.00", "10.00"),
                _bar("2026-08-14", "10.50", "10.20"),  # adjClose diverges here
            ]
        raise _not_found()

    monkeypatch.setattr(TiingoProvider, "_fetch_daily_bars", fake_fetch)
    provider = TiingoProvider(api_key="test-key")

    tiingo_cn_probe(provider, symbols=["600519.SS"], today=_TODAY)
    out = capsys.readouterr().out

    assert "✓ 600519.SS: format='600519-SHG'" in out
    assert "2 record(s)" in out
    assert "adjClose present=True" in out
    assert "adjClose ever != close=True" in out
    assert "latest bar=2026-08-14 vs latest XSHG session=2026-08-14 (same-day)" in out


def test_probe_reports_no_format_resolved_when_every_candidate_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(
        self: TiingoProvider, vendor_symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        raise _not_found()

    monkeypatch.setattr(TiingoProvider, "_fetch_daily_bars", fake_fetch)
    provider = TiingoProvider(api_key="test-key")

    tiingo_cn_probe(provider, symbols=["600519.SS"], today=_TODAY)  # must not raise
    out = capsys.readouterr().out

    assert "✗ 600519.SS: no candidate format resolved" in out
    assert "600519-SHG (HTTP 404)" in out


def test_probe_flags_stale_bar_as_behind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A latest bar dated before the latest XSHG session (a stale EOD print)
    reads BEHIND rather than same-day (CN-Q4)."""

    def fake_fetch(
        self: TiingoProvider, vendor_symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        return [_bar("2026-08-13", "10.00", "10.00")]  # one session stale

    monkeypatch.setattr(TiingoProvider, "_fetch_daily_bars", fake_fetch)
    provider = TiingoProvider(api_key="test-key")

    tiingo_cn_probe(provider, symbols=["600519.SS"], today=_TODAY)
    out = capsys.readouterr().out

    assert "latest bar=2026-08-13 vs latest XSHG session=2026-08-14 (BEHIND)" in out


def test_probe_never_raises_and_prints_a_line_per_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(
        self: TiingoProvider, vendor_symbol: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        return [_bar("2026-08-14", "10.00", "10.00")]

    monkeypatch.setattr(TiingoProvider, "_fetch_daily_bars", fake_fetch)
    provider = TiingoProvider(api_key="test-key")

    tiingo_cn_probe(provider, symbols=DEFAULT_SYMBOLS, today=_TODAY)
    out = capsys.readouterr().out

    for symbol in DEFAULT_SYMBOLS:
        assert symbol in out
    assert "CN-Q4" in out
