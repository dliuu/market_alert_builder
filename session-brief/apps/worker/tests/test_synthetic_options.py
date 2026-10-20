# tests/test_synthetic_options.py
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from worker.calendar import previous_session
from worker.providers.cboe import parse_chain, parse_index_csv
from worker.providers.synthetic import SyntheticOptionsProvider

SESSION = date(2026, 10, 20)


def _provider() -> SyntheticOptionsProvider:
    return SyntheticOptionsProvider({"AAPL": Decimal("200.00")}, SESSION)


def test_chain_parses_with_eight_expiries_and_two_sided_quotes() -> None:
    underlying, contracts = parse_chain(_provider().chain("AAPL"))
    assert underlying.close == Decimal("200.00")
    assert underlying.iv30 == Decimal("30.0")
    assert len({c.expiry for c in contracts}) >= 8
    assert all(c.expiry > SESSION for c in contracts)
    assert all(c.two_sided for c in contracts)
    assert {c.right for c in contracts} == {"C", "P"}


def test_chain_is_byte_identical_across_calls() -> None:
    a = json.dumps(_provider().chain("AAPL"), default=str)
    b = json.dumps(_provider().chain("AAPL"), default=str)
    assert a == b


def test_unknown_symbol_raises_key_error() -> None:
    with pytest.raises(KeyError):
        _provider().chain("MSFT")


def test_otm_call_carries_a_higher_iv_than_atm() -> None:
    _, contracts = parse_chain(_provider().chain("AAPL"))
    calls = [c for c in contracts if c.right == "C" and c.iv is not None]
    atm = next(c for c in calls if c.strike == Decimal("200.00"))
    otm = next(c for c in calls if c.strike == Decimal("220.00") and c.expiry == atm.expiry)
    assert atm.iv == Decimal("30.00")
    assert otm.iv == Decimal("34.00")


def test_index_history_parses_to_300_rows_ending_before_the_session() -> None:
    bars = parse_index_csv(_provider().index_history("VIX"))
    assert len(bars) == 300
    assert bars[-1].session_date <= previous_session(SESSION)
    assert bars[-1].session_date == previous_session(SESSION)
    assert all(b.o == b.h == b.l == b.c for b in bars)


def test_index_quote_close_in_range_and_shifted_for_vix3m() -> None:
    quote = _provider().index_quote("VIX")
    assert Decimal(15) <= quote["data"]["close"] <= Decimal(21)
    assert quote["data"]["options"] == []
    vix = _provider().index_quote("VIX")["data"]["close"]
    vix3m = _provider().index_quote("VIX3M")["data"]["close"]
    assert vix3m - vix == Decimal(1)
