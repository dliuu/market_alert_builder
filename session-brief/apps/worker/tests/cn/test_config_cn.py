"""Tests for Chinese-side worker config."""
from __future__ import annotations

import pytest

from worker_cn import config, constants


def test_cn_bars_are_synthetic_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """cn_bars_are_synthetic() returns True when CN_BARS_LIVE is not set."""
    monkeypatch.setattr(config, "CN_BARS_LIVE", False)
    assert config.cn_bars_are_synthetic() is True


def test_cn_bars_are_synthetic_false_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """cn_bars_are_synthetic() returns False when CN_BARS_LIVE is set truthy."""
    monkeypatch.setattr(config, "CN_BARS_LIVE", True)
    assert config.cn_bars_are_synthetic() is False


def test_cn_benchmark_constant() -> None:
    """CN_BENCHMARK matches the CSI 300 index symbol."""
    assert constants.CN_BENCHMARK == "000300.SS"
