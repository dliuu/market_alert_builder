"""TiingoProvider: parses the response and authenticates via header, never URL."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from worker.providers.tiingo import CN_TIINGO_FORMATS, TiingoProvider

_SAMPLE = (
    '[{"date":"2026-08-07T00:00:00.000Z","open":10.5,"high":11.0,"low":10.0,'
    '"close":10.8,"volume":1000,"adjClose":10.8}]'
)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_daily_bars_parses_and_authenticates_in_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse(_SAMPLE)

    monkeypatch.setattr("worker.providers.tiingo.httpx.get", fake_get)

    provider = TiingoProvider(api_key="secret-key")
    bars = provider.daily_bars("AAPL", date(2026, 8, 1), date(2026, 8, 7))

    assert len(bars) == 1
    # parse_float=Decimal keeps prices off the float path.
    assert bars[0]["close"] == Decimal("10.8")
    assert isinstance(bars[0]["close"], Decimal)

    assert captured["url"].endswith("/aapl/prices")
    assert captured["kwargs"]["headers"]["Authorization"] == "Token secret-key"
    assert captured["kwargs"]["params"] == {"startDate": "2026-08-01", "endDate": "2026-08-07"}
    # The key must never appear in the URL or query string.
    assert "secret-key" not in captured["url"]
    assert "secret-key" not in str(captured["kwargs"]["params"])


def test_missing_key_raises() -> None:
    with pytest.raises(RuntimeError, match="TIINGO_API_KEY"):
        TiingoProvider(api_key="")


# --- _vendor_symbol (CN-M3 Task 10) -----------------------------------------


def test_vendor_symbol_passes_us_symbols_through_unchanged() -> None:
    provider = TiingoProvider(api_key="secret-key")
    assert provider._vendor_symbol("AAPL") == "AAPL"


def test_vendor_symbol_maps_shanghai_suffix() -> None:
    # tiingo-cn-probe (2026-08-16 live run) confirmed the bare code resolves;
    # cn/docs/open-questions.md CN-Q1.
    provider = TiingoProvider(api_key="secret-key")
    assert provider._vendor_symbol("600519.SS") == "600519"
    assert CN_TIINGO_FORMATS[".SS"] == "{code}"


def test_vendor_symbol_maps_shenzhen_suffix() -> None:
    provider = TiingoProvider(api_key="secret-key")
    assert provider._vendor_symbol("300750.SZ") == "300750"
    assert CN_TIINGO_FORMATS[".SZ"] == "{code}"


def test_daily_bars_routes_cn_symbol_through_vendor_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        return _FakeResponse(_SAMPLE)

    monkeypatch.setattr("worker.providers.tiingo.httpx.get", fake_get)

    provider = TiingoProvider(api_key="secret-key")
    provider.daily_bars("600519.SS", date(2026, 8, 1), date(2026, 8, 7))

    assert captured["url"].endswith("/600519/prices")
