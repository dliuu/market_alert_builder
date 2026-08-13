from datetime import date

import pytest

from worker.baskets import Member, equal_weight_return, leave_one_out, members_on

M = [
    Member("A", date(2020, 1, 1), None),
    Member("B", date(2020, 1, 1), date(2021, 1, 1)),  # left the basket
    Member("C", date(2020, 6, 1), None),              # joined later
]


def test_members_on_honors_point_in_time():
    assert members_on(M, date(2020, 1, 1)) == ["A", "B"]
    assert members_on(M, date(2020, 6, 1)) == ["A", "B", "C"]
    assert members_on(M, date(2021, 6, 1)) == ["A", "C"]  # B gone


def test_equal_weight_return_is_the_mean():
    br = equal_weight_return({"A": 0.02, "B": -0.01, "C": 0.05})
    assert br.n_members == 3
    assert br.ret == pytest.approx((0.02 - 0.01 + 0.05) / 3)


def test_leave_one_out_excludes_the_named_symbol_exactly():
    rets = {"A": 0.02, "B": -0.01, "C": 0.05}
    full = equal_weight_return(rets).ret
    # Analytic LOO for A equals the direct mean of {B, C}.
    assert leave_one_out(full, 3, rets["A"]) == pytest.approx((-0.01 + 0.05) / 2)


def test_leave_one_out_undefined_below_two():
    with pytest.raises(ValueError):
        leave_one_out(0.03, 1, 0.03)
