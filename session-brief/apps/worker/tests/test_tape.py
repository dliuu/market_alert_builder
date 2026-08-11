"""Stage ③ tape quality: pure, no DB. RVOL and range position from daily OHLCV
(D3). The two invariants that bite (verify-numbers checks 5 and 6): range
position is bounded and handles h==l; the RVOL denominator excludes today."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from worker.tape import RVOL_WINDOW, tape_for_symbol


def _d(x: str) -> Decimal:
    return Decimal(x)


# --- range position -------------------------------------------------------


def test_range_position_midpoint() -> None:
    t = tape_for_symbol("A", h=_d("110"), l=_d("100"), c=_d("105"), v=1, prior_volumes=[])
    assert t.range_position == Fraction(1, 2)


def test_range_position_at_low_and_high() -> None:
    lo = tape_for_symbol("A", h=_d("110"), l=_d("100"), c=_d("100"), v=1, prior_volumes=[])
    hi = tape_for_symbol("A", h=_d("110"), l=_d("100"), c=_d("110"), v=1, prior_volumes=[])
    assert lo.range_position == Fraction(0)
    assert hi.range_position == Fraction(1)


def test_range_position_null_when_high_equals_low() -> None:
    # A doji / halted bar: h == l → division by zero must yield null, not crash.
    t = tape_for_symbol("A", h=_d("100"), l=_d("100"), c=_d("100"), v=1, prior_volumes=[])
    assert t.range_position is None


def test_range_position_is_bounded() -> None:
    t = tape_for_symbol("A", h=_d("100"), l=_d("99.5"), c=_d("99.9"), v=1, prior_volumes=[])
    assert t.range_position is not None
    assert 0 <= t.range_position <= 1


# --- RVOL -----------------------------------------------------------------


def test_rvol_double_average() -> None:
    prior = [100] * RVOL_WINDOW
    t = tape_for_symbol("A", h=_d("1"), l=_d("1"), c=_d("1"), v=200, prior_volumes=prior)
    assert t.rvol == Fraction(2)


def test_rvol_boundary_is_exact() -> None:
    # 150 / 100 == 1.5 exactly — not > 1.5, so it must NOT trip the full-tier rule.
    prior = [100] * RVOL_WINDOW
    t = tape_for_symbol("A", h=_d("1"), l=_d("1"), c=_d("1"), v=150, prior_volumes=prior)
    assert t.rvol == Fraction(3, 2)


def test_rvol_null_without_enough_history() -> None:
    prior = [100] * (RVOL_WINDOW - 1)  # one short of the window
    t = tape_for_symbol("A", h=_d("1"), l=_d("1"), c=_d("1"), v=200, prior_volumes=prior)
    assert t.rvol is None


def test_rvol_excludes_today() -> None:
    # today's own volume (500) must not enter the denominator; only the 30 prior.
    prior = [100] * RVOL_WINDOW
    t = tape_for_symbol("A", h=_d("1"), l=_d("1"), c=_d("1"), v=500, prior_volumes=prior)
    assert t.rvol == Fraction(5)  # 500 / mean(prior)=100, not 500 / mean(incl today)


def test_rvol_uses_only_the_most_recent_window() -> None:
    # 40 prior sessions; only the most recent 30 count. Older 10 are huge but ignored.
    prior = [10_000] * 10 + [100] * RVOL_WINDOW
    t = tape_for_symbol("A", h=_d("1"), l=_d("1"), c=_d("1"), v=200, prior_volumes=prior)
    assert t.rvol == Fraction(2)
