# tests/test_config_options.py
from __future__ import annotations

import importlib

import pytest

from worker import config


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("true", True), ("TRUE", True), ("1", True), ("yes", False)],
)
def test_options_live_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("OPTIONS_LIVE", raising=False)
    else:
        monkeypatch.setenv("OPTIONS_LIVE", value)
    try:
        assert importlib.reload(config).OPTIONS_LIVE is expected
    finally:
        monkeypatch.undo()
        importlib.reload(config)
