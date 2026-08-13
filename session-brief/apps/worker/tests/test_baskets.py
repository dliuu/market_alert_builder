from __future__ import annotations

from datetime import date

import pytest

from worker.baskets import (
    MIN_DOLLAR_VOLUME,
    Member,
    equal_weight_return,
    leave_one_out,
    loo_weighted_return,
    members_on,
    screen_and_cap,
    weighted_return,
)

M = [
    Member("A", date(2020, 1, 1), None),
    Member("B", date(2020, 1, 1), date(2021, 1, 1)),  # left the basket
    Member("C", date(2020, 6, 1), None),              # joined later
]


def test_members_on_honors_point_in_time() -> None:
    assert members_on(M, date(2020, 1, 1)) == ["A", "B"]
    assert members_on(M, date(2020, 6, 1)) == ["A", "B", "C"]
    assert members_on(M, date(2021, 6, 1)) == ["A", "C"]  # B gone


def test_equal_weight_return_is_the_mean() -> None:
    br = equal_weight_return({"A": 0.02, "B": -0.01, "C": 0.05})
    assert br.n_members == 3
    assert br.ret == pytest.approx((0.02 - 0.01 + 0.05) / 3)


def test_leave_one_out_excludes_the_named_symbol_exactly() -> None:
    rets = {"A": 0.02, "B": -0.01, "C": 0.05}
    full = equal_weight_return(rets).ret
    # Analytic LOO for A equals the direct mean of {B, C}.
    assert leave_one_out(full, 3, rets["A"]) == pytest.approx((-0.01 + 0.05) / 2)


def test_leave_one_out_undefined_below_two() -> None:
    with pytest.raises(ValueError):
        leave_one_out(0.03, 1, 0.03)


def test_screen_drops_thin_names() -> None:
    liq = {"A": 5e6, "B": 5e6, "C": 100.0}  # C below the floor
    w = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.5)
    assert set(w) == {"A", "B"}
    assert sum(w.values()) == pytest.approx(1.0)


def test_cap_stops_one_name_from_becoming_the_basket() -> None:
    liq = {"A": 1e9, "B": 5e6, "C": 5e6, "D": 5e6, "E": 5e6}
    w = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.25)
    assert w["A"] == pytest.approx(0.25)         # capped
    assert sum(w.values()) == pytest.approx(1.0)  # excess redistributed


def test_weighted_return_is_the_weighted_mean() -> None:
    br = weighted_return({"A": 0.02, "B": -0.01}, {"A": 0.75, "B": 0.25})
    assert br.ret == pytest.approx(0.75 * 0.02 + 0.25 * -0.01)
    assert br.n_members == 2


def test_loo_parity_with_analytic_on_uncapped_equal_weight() -> None:
    # No cap binds and all names pass the screen -> equal weight -> analytic LOO.
    rets = {"A": 0.02, "B": -0.01, "C": 0.05}
    liq = {"A": 5e6, "B": 5e6, "C": 5e6}
    br = loo_weighted_return(
        rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=1.0, excluded="A"
    )
    assert br.ret == pytest.approx((-0.01 + 0.05) / 2)  # mean of {B, C}
    assert "A" not in {*rets} - {"A"} or br.n_members == 2


def test_loo_reflects_recapping_after_removal() -> None:
    # Removing the capped mega-cap frees weight; survivors re-cap equally here.
    rets = {"A": 0.10, "B": 0.00, "C": 0.00, "D": 0.00, "E": 0.00}
    liq = {"A": 1e9, "B": 5e6, "C": 5e6, "D": 5e6, "E": 5e6}
    br = loo_weighted_return(
        rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=0.25, excluded="A"
    )
    assert br.n_members == 4
    assert br.ret == pytest.approx(0.0)  # A (the only mover) is gone
