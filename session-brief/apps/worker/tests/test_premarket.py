"""The pre-market feed seam and its pure math (M15).

Everything here runs without a network or a database: the synthetic provider is
deterministic by construction, which is what lets a seeded pre-market session be
snapshot-tested (the M7 fundamentals / M14 events precedent).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from worker.providers.synthetic import SyntheticPremarketProvider

_SESSION = date(2026, 8, 13)
_CLOSES = {"SNDK": Decimal("47.32"), "SYM": Decimal("36.10"), "ES=F": Decimal("5635.25")}


def test_synthetic_latest_prices_are_deterministic() -> None:
    """Same symbols, same session → byte-identical output. A seeded feed that
    drifted between runs would make every snapshot test flaky."""
    a = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK", "SYM"])
    b = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK", "SYM"])
    assert a == b


def test_synthetic_latest_prices_shape() -> None:
    (row,) = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK"])
    assert set(row) == {"symbol", "extended_last", "extended_v", "prev_close"}
    assert isinstance(row["extended_last"], Decimal)  # never float on the price path
    assert isinstance(row["extended_v"], int)
    assert row["prev_close"] == _CLOSES["SNDK"]


def test_synthetic_moves_with_the_session() -> None:
    """A different morning gaps differently — otherwise every seeded session
    tells the same story and the threshold is never exercised."""
    today = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK"])
    other = SyntheticPremarketProvider(_CLOSES, date(2026, 8, 14)).get_latest_prices(["SNDK"])
    assert today[0]["extended_last"] != other[0]["extended_last"]


def test_synthetic_tape_shape() -> None:
    (row,) = SyntheticPremarketProvider(_CLOSES, _SESSION).get_futures_prices(["ES=F"])
    assert set(row) == {"symbol", "last", "prev_close"}
    assert isinstance(row["last"], Decimal)


def test_symbol_without_a_prior_close_is_skipped() -> None:
    """No prior close, no gap — a name the book just added has nothing to
    measure against, and inventing a base would invent a move."""
    assert SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["NEW"]) == []


def test_synthetic_provider_satisfies_the_protocol() -> None:
    """The point of the seam: the seed and the licensed feed are interchangeable."""
    from worker.providers.base import MarketDataProvider

    provider: MarketDataProvider = SyntheticPremarketProvider(
        {"SNDK": Decimal("47.32")}, _SESSION
    )
    (pre,) = provider.get_latest_prices(["SNDK"])
    assert set(pre) == {"symbol", "extended_last",
                        "extended_v", "prev_close"}
    for fetch in (provider.get_futures_prices,
                  provider.get_index_quotes,
                  provider.get_forex_quotes):
        (row,) = fetch(["SNDK"])
        assert set(row) == {"symbol", "last", "prev_close"}
