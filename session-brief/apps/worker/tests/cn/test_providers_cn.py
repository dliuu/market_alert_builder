"""Tests for SyntheticCnBarsProvider (CN-M1). Pure — no DB, no network."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from worker.providers.tiingo import TiingoProvider
from worker_cn import config as cn_config
from worker_cn.providers import SyntheticCnBarsProvider, default_cn_bars_provider

_SYMBOL = "600519.SS"


def test_bar_is_deterministic_and_window_independent() -> None:
    """The bar for (symbol, d) is a pure function of (symbol, d): two
    different backfill windows produce a byte-identical bar for the same
    day."""
    provider = SyntheticCnBarsProvider()
    d = date(2026, 8, 14)  # a plain Friday XSHG session

    wide = provider.daily_bars(_SYMBOL, d - timedelta(days=10), d)
    narrow = provider.daily_bars(_SYMBOL, d - timedelta(days=1), d)

    wide_bar = next(r for r in wide if r["date"].startswith(d.isoformat()))
    narrow_bar = next(r for r in narrow if r["date"].startswith(d.isoformat()))
    assert wide_bar == narrow_bar


def test_no_bar_on_national_day() -> None:
    """2026-10-01 (National Day / Golden Week) is not an XSHG session, so no
    window covering it should contain a bar dated that day."""
    provider = SyntheticCnBarsProvider()
    d = date(2026, 10, 1)

    records = provider.daily_bars(_SYMBOL, d - timedelta(days=5), d + timedelta(days=5))

    assert all(not r["date"].startswith(d.isoformat()) for r in records)


def test_bar_sanity_ohlc_and_types() -> None:
    provider = SyntheticCnBarsProvider()
    start, end = date(2026, 8, 3), date(2026, 8, 14)

    records = provider.daily_bars(_SYMBOL, start, end)

    assert records  # the window contains XSHG sessions
    for record in records:
        o, h, low, c = record["open"], record["high"], record["low"], record["close"]
        for field in (o, h, low, c, record["adjClose"]):
            assert isinstance(field, Decimal)
        assert h >= max(o, c) >= min(o, c) >= low
        assert record["adjClose"] == c
        assert isinstance(record["volume"], int)
        assert record["volume"] > 0


# --- default_cn_bars_provider (CN-M3 Task 10) -------------------------------


def test_default_cn_bars_provider_is_synthetic_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cn_config, "CN_BARS_LIVE", False)
    assert isinstance(default_cn_bars_provider(), SyntheticCnBarsProvider)


def test_default_cn_bars_provider_is_tiingo_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cn_config, "CN_BARS_LIVE", True)
    monkeypatch.setattr("worker.providers.tiingo.TIINGO_API_KEY", "test-key")
    assert isinstance(default_cn_bars_provider(), TiingoProvider)
