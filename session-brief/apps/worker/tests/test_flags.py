"""M7 flags: pure threshold logic, no DB. Each mechanism fires at/over its
threshold (docs/05) and stays silent under it — the "thresholds fire correctly
on a synthetic fixture" half of the DoD. The weekly rate limit is DB-backed and
covered by test_flags_db.py."""

from __future__ import annotations

from fractions import Fraction

from worker.flags import (
    concentration_flags,
    correlation_flag,
    dilution_flag,
    dilution_yoy,
    earnings_soon_flag,
    mean_pairwise_corr,
    runway_flag,
    runway_quarters,
    short_interest_flag,
    supply_event_flag,
)

# --- Numeric helpers ------------------------------------------------------


def test_runway_quarters_is_cash_over_mean_burn() -> None:
    # cash 1200, burns [100, 200, 300, 400] → mean 250 → 4.8 quarters.
    assert runway_quarters(1200, [100, 200, 300, 400]) == Fraction(48, 10)


def test_runway_quarters_undefined_without_burn() -> None:
    assert runway_quarters(1200, []) is None
    assert runway_quarters(1200, [0, 0]) is None  # mean burn 0 → no denominator


def test_dilution_yoy_is_share_growth() -> None:
    # 115M now vs 100M a year ago → +15%.
    assert dilution_yoy(115, 100) == Fraction(15, 100)


def test_dilution_yoy_undefined_without_base() -> None:
    assert dilution_yoy(115, 0) is None


def test_mean_pairwise_corr_of_lockstep_series_is_one() -> None:
    series = {"A": [0.01, -0.02, 0.03, -0.01], "B": [0.02, -0.04, 0.06, -0.02]}
    corr = mean_pairwise_corr(series)
    assert corr is not None and abs(corr - 1.0) < 1e-9


def test_mean_pairwise_corr_needs_two_varying_series() -> None:
    assert mean_pairwise_corr({"A": [0.01, 0.02, 0.03]}) is None  # one series
    assert mean_pairwise_corr({"A": [1, 1, 1], "B": [0.1, 0.2, 0.3]}) is None  # A flat


# --- Position-risk thresholds ---------------------------------------------


def test_runway_flag_fires_below_six_quarters() -> None:
    assert runway_flag("A", Fraction(59, 10)) is not None  # 5.9q < 6
    assert runway_flag("A", Fraction(6)) is None  # exactly 6 is not < 6
    assert runway_flag("A", None) is None


def test_dilution_flag_fires_above_fifteen_percent() -> None:
    assert dilution_flag("A", Fraction(16, 100)) is not None
    assert dilution_flag("A", Fraction(15, 100)) is None  # exactly 15% is not > 15%
    assert dilution_flag("A", None) is None


def test_earnings_soon_flag_fires_inside_the_window() -> None:
    assert earnings_soon_flag("A", 7) is not None  # within 7 days
    assert earnings_soon_flag("A", 8) is None
    assert earnings_soon_flag("A", None) is None
    assert earnings_soon_flag("A", -1) is None  # already past


def test_supply_event_flag_fires_inside_seven_days() -> None:
    assert supply_event_flag("A", 7) is not None
    assert supply_event_flag("A", 8) is None
    assert supply_event_flag("A", None) is None


def test_short_interest_flag_fires_above_twenty_percent() -> None:
    assert short_interest_flag("A", Fraction(21, 100)) is not None
    assert short_interest_flag("A", Fraction(20, 100)) is None
    assert short_interest_flag("A", None) is None


# --- Correlation flag (weekly-capped mechanism) ---------------------------


def test_name_concentration_fires_above_twenty_percent() -> None:
    flags = concentration_flags({"A": Fraction(25, 100), "B": Fraction(10, 100)}, {})
    assert [f.symbol for f in flags] == ["A"]
    assert flags[0].type == "concentration"
    assert flags[0].text_key == "single_name_concentration"


def test_sector_concentration_fires_above_fifty_percent() -> None:
    flags = concentration_flags({}, {"sec1": Fraction(55, 100), "sec2": Fraction(20, 100)})
    assert [f.sector_id for f in flags] == ["sec1"]
    assert flags[0].text_key == "sector_concentration"


def test_concentration_boundaries_are_exclusive() -> None:
    assert concentration_flags({"A": Fraction(20, 100)}, {"s": Fraction(50, 100)}) == []


def test_correlation_flag_fires_above_threshold() -> None:
    assert correlation_flag(0.76) is not None
    assert correlation_flag(0.75) is None  # exactly 0.75 is not > 0.75
    assert correlation_flag(None) is None
    assert correlation_flag(0.76).text_key == "corr_20d_high"  # type: ignore[union-attr]
