"""``signed_money`` — currency-aware money formatting for subject lines. Pins
USD's shape against ``assemble._signed_dollars`` (which it generalizes) and
CNY's shape by literal, plus the negative/zero/unknown-currency cases."""

from __future__ import annotations

import pytest

from worker import assemble
from worker.assemble_shared import signed_money


@pytest.mark.parametrize("cents", [174610, -174610, 0, 100, -1, 123456789])
def test_signed_money_usd_matches_signed_dollars(cents: int) -> None:
    assert signed_money(cents, "USD") == assemble._signed_dollars(cents / 100)


def test_signed_money_usd_positive_shape() -> None:
    assert signed_money(174610, "USD") == "+$1,746.10"


def test_signed_money_usd_negative_shape() -> None:
    assert signed_money(-174610, "USD") == "-$1,746.10"


def test_signed_money_usd_zero_is_positive_sign() -> None:
    assert signed_money(0, "USD") == "+$0.00"


def test_signed_money_cny_positive_shape() -> None:
    assert signed_money(174610, "CNY") == "+¥1,746.10"


def test_signed_money_cny_negative_shape() -> None:
    assert signed_money(-174610, "CNY") == "-¥1,746.10"


def test_signed_money_defaults_to_usd() -> None:
    assert signed_money(174610) == "+$1,746.10"


def test_signed_money_unknown_currency_raises() -> None:
    with pytest.raises(ValueError, match="JPY"):
        signed_money(100, "JPY")
