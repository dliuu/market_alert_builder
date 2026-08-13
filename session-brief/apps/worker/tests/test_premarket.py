"""The pre-market feed seam and its pure math (M15).

Everything here runs without a network or a database: the synthetic provider is
deterministic by construction, which is what lets a seeded pre-market session be
snapshot-tested (the M7 fundamentals / M14 events precedent).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from worker.premarket import (
    PremarketQuote,
    clears_threshold,
    gap_cents,
    pre_pct,
    premarket_vol_mult,
)
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


def _q(last: str, prev: str, v: int = 100_000, typical: str | None = "50000") -> PremarketQuote:
    return PremarketQuote(
        symbol="SNDK",
        extended_last=Decimal(last),
        extended_v=v,
        prev_close=Decimal(prev),
        typical_v=Decimal(typical) if typical is not None else None,
    )


def test_pre_pct_is_extended_last_against_the_prior_close() -> None:
    assert pre_pct(_q("50.00", "40.00")) == Decimal("0.25")


def test_pre_pct_is_none_without_a_base() -> None:
    assert pre_pct(_q("50.00", "0")) is None


def test_gap_is_dollars_in_integer_cents() -> None:
    """The dollars-not-percent rule (docs/01): +$1.94 on a 47.32 close."""
    assert gap_cents(_q("49.26", "47.32")) == 194
    assert gap_cents(_q("46.00", "47.32")) == -132


def test_volume_multiple_is_against_typical_premarket_volume() -> None:
    """Not the 30-day RVOL: the base is prior sessions' pre-market volume at the
    same point in the morning, because pre-market volume is too thin for a
    daily-volume ratio to mean anything (D3, docs/05)."""
    assert premarket_vol_mult(_q("50.00", "49.00", v=150_000, typical="50000")) == Decimal("3")


def test_volume_multiple_is_none_without_enough_history() -> None:
    assert premarket_vol_mult(_q("50.00", "49.00", typical=None)) is None


def test_threshold_takes_names_over_one_percent() -> None:
    assert clears_threshold(_q("48.00", "47.32")) is True    # +1.4%
    assert clears_threshold(_q("47.50", "47.32")) is False   # +0.4%
    assert clears_threshold(_q("46.60", "47.32")) is True    # -1.5%, direction-blind


def test_news_clears_the_threshold_on_its_own() -> None:
    """docs/05: ">1% pre-market **or** carrying news". No news feed exists
    (docs/02 marks it Premium and unwired), so the predicate takes the flag and
    nothing sets it yet — the D18 short_interest precedent."""
    assert clears_threshold(_q("47.35", "47.32"), has_news=True) is True
