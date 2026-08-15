# tests/test_config_fdn.py
from __future__ import annotations

import pytest

from worker import config


def test_synthetic_flag_derives_from_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "FDN_API_KEY", "")
    assert config.premarket_feed_is_synthetic() is True
    monkeypatch.setattr(config, "FDN_API_KEY", "fdn_test_key")
    assert config.premarket_feed_is_synthetic() is False
