from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from worker.options import Contract, parse_occ


def test_parse_occ_handles_short_and_long_roots_and_puts() -> None:
    aapl = parse_occ("AAPL261121C00340000")
    assert (aapl.root, aapl.expiry, aapl.right, aapl.strike) == (
        "AAPL",
        date(2026, 11, 21),
        "C",
        Decimal("340"),
    )
    brkb = parse_occ("BRKB261121P00012500")
    assert (brkb.root, brkb.right, brkb.strike) == ("BRKB", "P", Decimal("12.5"))
    six = parse_occ("SPXW261121C04500000")
    assert (six.root, six.strike) == ("SPXW", Decimal("4500"))


@pytest.mark.parametrize(
    "bad", ["AAPL", "AAPL261121X00340000", "261121C00340000", "AAPL2611C0034000"]
)
def test_parse_occ_rejects_non_occ_symbols(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_occ(bad)


def _contract(bid: str, ask: str) -> Contract:
    return Contract(
        symbol="TEST261121C00100000",
        expiry=date(2026, 11, 21),
        right="C",
        strike=Decimal(100),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=1,
        ask_size=1,
        iv=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
        open_interest=0,
        volume=0,
        last=None,
        prev_day_close=None,
        percent_change=None,
    )


def test_two_sided_needs_a_bid_and_an_ask_above_it() -> None:
    assert _contract("1.00", "1.10").two_sided
    assert not _contract("0", "1.10").two_sided
    assert not _contract("1.00", "1.00").two_sided
    assert _contract("1.00", "1.10").mid == Decimal("1.05")
