"""The pure compute core: every figure, and the exact sum identities. No DB."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

from worker.compute import ComputeResult, Lot, Price, compute

_SESSION = date(2026, 8, 11)


def _lot(symbol: str, shares: str, cost: str, opened: date = date(2026, 8, 1)) -> Lot:
    # cost is dollars-per-share; store as integer cents.
    return Lot(
        symbol=symbol,
        shares=Decimal(shares),
        cost_basis_cents=int((Decimal(cost) * 100).to_integral_value()),
        opened_on=opened,
    )


def _price(c: str, prev_c: str | None) -> Price:
    return Price(c=Decimal(c), prev_c=Decimal(prev_c) if prev_c is not None else None)


# --- Individual calculations ---------------------------------------------


def test_day_return() -> None:
    result = compute(_SESSION, [_lot("A", "1", "1")], {"A": _price("110", "100")})
    assert result.positions[0].day_return == Fraction(1, 10)


def test_day_return_none_without_prev_bar() -> None:
    # A lot opened today with no prior close: day_return is undefined.
    lot = _lot("A", "1", "100", opened=_SESSION)
    result = compute(_SESSION, [lot], {"A": _price("110", None)})
    assert result.positions[0].day_return is None


def test_position_value_and_pnl() -> None:
    lot = _lot("A", "10", "90")  # held since Aug 1
    result = compute(_SESSION, [lot], {"A": _price("110", "100")})
    pos = result.positions[0]
    assert pos.value_cents == 110_000  # 10 * $110 = $1,100
    assert pos.prior_value_cents == 100_000  # 10 * $100 (prev close) = $1,000
    assert pos.day_pnl_cents == 10_000  # 10 * ($110 - $100) = $100
    assert pos.total_pnl_cents == 20_000  # 10 * ($110 - $90) = $200
    assert pos.total_cost_cents == 90_000  # 10 * $90 = $900


def test_lot_opened_today_uses_cost_not_prev_close() -> None:
    # Held lot plus a lot bought today; today's lot has no prior value and its
    # day P&L is measured from the purchase price.
    held = _lot("A", "10", "90")
    fresh = _lot("A", "5", "105", opened=_SESSION)
    result = compute(_SESSION, [held, fresh], {"A": _price("110", "100")})
    pos = result.positions[0]
    assert pos.prior_value_cents == 100_000  # only the held lot: 10 * $100 = $1,000
    # day P&L: held 10*($110-$100)=+$100, fresh 5*($110-$105)=+$25 → $125
    assert pos.day_pnl_cents == 12_500


def test_fractional_shares_round_to_cents() -> None:
    lot = _lot("A", "1.5", "10")
    result = compute(_SESSION, [lot], {"A": _price("10.333", "10")})
    # 1.5 * $10.333 = $15.4995 → rounds half-up to $15.50
    assert result.positions[0].value_cents == 1550


# --- Book aggregates ------------------------------------------------------


def _two_book() -> ComputeResult:
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40")]
    prices = {"A": _price("110", "100"), "B": _price("48", "50")}
    return compute(_SESSION, lots, prices)


def test_book_is_sum_of_positions() -> None:
    result = _two_book()
    book, positions = result.book, result.positions
    assert book.value_cents == sum(p.value_cents for p in positions)
    assert book.day_pnl_cents == sum(p.day_pnl_cents for p in positions)
    assert book.total_pnl_cents == sum(p.total_pnl_cents for p in positions)


def test_total_pct() -> None:
    book = _two_book().book
    # total P&L: A 10*($110-$90)=$200, B 20*($48-$40)=$160 → $360; cost $900+$800=$1700
    assert book.total_pnl_cents == 360_00
    assert book.total_cost_cents == 1700_00
    assert book.total_pct == Fraction(360_00, 1700_00)


def test_vs_spy() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": _price("110", "100")}
    spy_return = Fraction(1, 100)  # +1%
    result = compute(_SESSION, lots, prices, benchmark_return=spy_return)
    # book day_bps = $100 day P&L / $1,000 prior = 10% = 1000 bps; SPY = +1% = 100 bps
    assert result.book.day_bps == Fraction(1000)
    assert result.book.vs_spy_bps == Fraction(1000) - Fraction(1, 100) * 10_000  # 900 bps


# --- The invariant: exact sum identities ---------------------------------


def test_contribution_sums_to_day_bps_exactly() -> None:
    # Deliberately messy: P&L that does not divide the book value evenly, so the
    # identity would fail under rounded-decimal arithmetic but holds with Fractions.
    lots = [_lot("A", "7", "31.11"), _lot("B", "13", "17.77"), _lot("C", "3", "99.01")]
    prices = {
        "A": _price("33.33", "30.00"),
        "B": _price("18.18", "19.00"),
        "C": _price("101.23", "97.50"),
    }
    result = compute(_SESSION, lots, prices)

    total = sum(
        (p.contribution_bps for p in result.positions if p.contribution_bps is not None),
        Fraction(0),
    )
    assert total == result.book.day_bps


def test_weights_sum_to_one_exactly() -> None:
    lots = [_lot("A", "7", "31.11"), _lot("B", "13", "17.77"), _lot("C", "3", "99.01")]
    prices = {
        "A": _price("33.33", "30.00"),
        "B": _price("18.18", "19.00"),
        "C": _price("101.23", "97.50"),
    }
    result = compute(_SESSION, lots, prices)
    total = sum((p.weight for p in result.positions if p.weight is not None), Fraction(0))
    assert total == Fraction(1)


# --- Guards ---------------------------------------------------------------


def test_no_positions_gives_zero_book_and_no_bps() -> None:
    result = compute(_SESSION, [], {})
    assert result.positions == []
    assert result.book.value_cents == 0
    assert result.book.day_bps is None
    assert result.book.total_pct is None


def test_whole_book_opened_today_has_no_day_bps() -> None:
    lot = _lot("A", "10", "100", opened=_SESSION)
    result = compute(_SESSION, [lot], {"A": _price("110", None)})
    assert result.book.prior_value_cents == 0
    assert result.book.day_bps is None
    assert result.positions[0].contribution_bps is None


def test_missing_prev_close_for_held_lot_raises() -> None:
    lot = _lot("A", "10", "90")  # held since Aug 1
    try:
        compute(_SESSION, [lot], {"A": _price("110", None)})
    except ValueError as exc:
        assert "prev_close" in str(exc)
    else:
        raise AssertionError("expected ValueError for a held lot with no prior close")
