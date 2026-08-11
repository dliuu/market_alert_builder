"""M6 claim emission: pure, no DB. A full-tier name that beat (or lagged) the
benchmark by more than the threshold earns a relative_strength claim; quiet or
brief-tier names earn none. Resolution is grade-from-the-tape and is covered by
the DB test."""

from __future__ import annotations

from fractions import Fraction

from worker.claims import emit_claims
from worker.compute import PositionMetrics


def _pos(symbol: str, day_return: Fraction | None) -> PositionMetrics:
    return PositionMetrics(
        symbol=symbol,
        day_return=day_return,
        value_cents=0,
        prior_value_cents=0,
        day_pnl_cents=0,
        total_pnl_cents=0,
        total_cost_cents=0,
        contribution_bps=None,
        weight=None,
    )


def test_outperformer_earns_an_up_claim() -> None:
    # +5% vs benchmark +1% → +4% relative, over the 1% threshold → up.
    shown = [(_pos("A", Fraction(5, 100)), "full")]
    claims = emit_claims(shown, Fraction(1, 100))
    assert len(claims) == 1
    assert claims[0].symbol == "A"
    assert claims[0].claim_type == "relative_strength"
    assert claims[0].direction == "up"
    assert claims[0].horizon_sessions == 1


def test_underperformer_earns_a_down_claim() -> None:
    # -3% vs benchmark +1% → -4% relative → down.
    shown = [(_pos("A", Fraction(-3, 100)), "full")]
    claims = emit_claims(shown, Fraction(1, 100))
    assert claims[0].direction == "down"


def test_move_inside_the_threshold_earns_nothing() -> None:
    # +1.5% vs +1% → +0.5% relative, under the 1% threshold.
    shown = [(_pos("A", Fraction(15, 1000)), "full")]
    assert emit_claims(shown, Fraction(1, 100)) == []


def test_brief_tier_names_are_never_claimed() -> None:
    # Big relative move, but the name is brief-tier (not a headline mover).
    shown = [(_pos("A", Fraction(5, 100)), "brief")]
    assert emit_claims(shown, Fraction(1, 100)) == []


def test_no_benchmark_no_claims() -> None:
    shown = [(_pos("A", Fraction(5, 100)), "full")]
    assert emit_claims(shown, None) == []


def test_threshold_boundary_is_exclusive() -> None:
    # Exactly +1% relative is not > 1% → no claim.
    shown = [(_pos("A", Fraction(2, 100)), "full")]
    assert emit_claims(shown, Fraction(1, 100)) == []
