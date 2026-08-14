"""Stage ④ assemble: pure, no DB. Figure checks on a hand-verifiable book, the
M5 tiering rules (full / brief / suppressed), and a snapshot of a mixed session
against a frozen fixture (docs/04)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from contracts.brief import BriefObject, Row
from worker.assemble import SCHEMA_VERSION, assemble, close_brief_should_skip
from worker.compute import Lot, Price, compute
from worker.tape import TapeMetrics


def _tier_of(row: Row) -> str:
    assert row.tier is not None  # attribution rows are always tiered
    return row.tier.value


_SESSION = date(2026, 8, 11)  # a Tuesday
_USER = "00000000-0000-0000-0000-000000000001"
_GENERATED_AT = datetime(2026, 8, 11, 20, 42, 11, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief.json"


def _lot(symbol: str, shares: str, cost: str) -> Lot:
    cost_cents = int((Decimal(cost) * 100).to_integral_value())
    return Lot(symbol, Decimal(shares), cost_cents, date(2026, 8, 1))


def _tape(symbol: str, rvol: str | None, rp: str | None) -> TapeMetrics:
    return TapeMetrics(
        symbol=symbol,
        rvol=Fraction(rvol) if rvol is not None else None,
        range_position=Fraction(rp) if rp is not None else None,
    )


# --- Two-name book, both movers: hand-checkable figures -------------------


def _two() -> BriefObject:
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40")]
    prices = {
        "A": Price(c=Decimal("110"), prev_c=Decimal("100")),  # +10%
        "B": Price(c=Decimal("48"), prev_c=Decimal("50")),  # -4%
    }
    closes = {"A": Decimal("110"), "B": Decimal("48")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT)


def test_book_totals() -> None:
    book = _two().book
    # `book` is nullable since v3 (the open brief omits it) — a close brief
    # always has one, and asserting that is part of what this test checks.
    assert book is not None
    assert book.value_cents == 206_000
    assert book.day_pnl_cents == 6_000
    assert book.day_bps == 300
    assert book.total_pnl_cents == 36_000
    assert book.vs_spy_bps == 200


def test_both_movers_are_full_none_suppressed() -> None:
    obj = _two()
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    assert [r.symbol for r in attribution.rows] == ["A", "B"]
    assert all(_tier_of(r) == "full" for r in attribution.rows)
    assert obj.suppressed == []


# --- Mixed session: one of each tier (the M5 snapshot) --------------------


def _mixed() -> BriefObject:
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40"), _lot("C", "5", "100")]
    prices = {
        "A": Price(c=Decimal("110"), prev_c=Decimal("100")),  # +10.0% → full
        "B": Price(c=Decimal("49.75"), prev_c=Decimal("50")),  # -0.5%  → brief
        "C": Price(c=Decimal("100.1"), prev_c=Decimal("100")),  # +0.1%  → suppressed
    }
    closes = {"A": Decimal("110"), "B": Decimal("49.75"), "C": Decimal("100.1")}
    tape = {
        "A": _tape("A", "2", "0.8"),
        "B": _tape("B", "1.2", "0.5"),
        "C": _tape("C", "1", "0.5"),
    }
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(result, closes, tape, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT)


def test_tiers_partition_every_name() -> None:
    obj = _mixed()
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    tiers = {r.symbol: _tier_of(r) for r in attribution.rows}
    assert tiers == {"A": "full", "B": "brief"}
    assert obj.suppressed == ["C"]
    # No name is lost: shown ∪ suppressed == every held symbol.
    assert set(tiers) | set(obj.suppressed) == {"A", "B", "C"}


def test_tape_quality_is_full_movers_only() -> None:
    obj = _mixed()
    tape = next(s for s in obj.sections if s.id.value == "tape_quality")
    assert [r.symbol for r in tape.rows] == ["A"]  # B is brief, C suppressed
    assert tape.rows[0].rvol == 2.0
    assert tape.rows[0].range_position == 0.8


def test_mixed_session_sends() -> None:
    assert close_brief_should_skip(_mixed()) is False  # A is a full-tier mover


def test_matches_frozen_fixture() -> None:
    got = _mixed().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text())
    assert got == expected


# --- Quiet session: nothing moved >1% → skip ------------------------------


def _quiet() -> BriefObject:
    # B brief (-0.5%) and C suppressed (+0.1%); no full-tier mover.
    lots = [_lot("B", "20", "40"), _lot("C", "5", "100")]
    prices = {
        "B": Price(c=Decimal("49.75"), prev_c=Decimal("50")),
        "C": Price(c=Decimal("100.1"), prev_c=Decimal("100")),
    }
    closes = {"B": Decimal("49.75"), "C": Decimal("100.1")}
    tape = {"B": _tape("B", "1.2", "0.5"), "C": _tape("C", "1", "0.5")}
    result = compute(_SESSION, lots, prices)
    return assemble(result, closes, tape, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT)


def test_quiet_session_is_skipped() -> None:
    assert close_brief_should_skip(_quiet()) is True


def test_rvol_spike_promotes_a_flat_name_to_full() -> None:
    # C barely moved (+0.1%) but its volume spiked → full, so the brief sends.
    lots = [_lot("C", "5", "100")]
    prices = {"C": Price(c=Decimal("100.1"), prev_c=Decimal("100"))}
    closes = {"C": Decimal("100.1")}
    tape = {"C": _tape("C", "3", "0.5")}  # rvol 3.0 > 1.5
    result = compute(_SESSION, lots, prices)
    obj = assemble(result, closes, tape, user_id=_USER, session_date=_SESSION,
                   kind="close", generated_at=_GENERATED_AT)
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    assert _tier_of(attribution.rows[0]) == "full"
    assert obj.suppressed == []
    assert close_brief_should_skip(obj) is False


def test_schema_version_is_four() -> None:
    # v4 = M13 attribution decomp on top of M14's v3 open-brief shape (docs/04).
    assert _mixed().schema_version == SCHEMA_VERSION == 4


def test_material_residual_predicate() -> None:
    from worker.attribution import material_residual

    assert material_residual(2.5) is True
    assert material_residual(-2.0) is True
    assert material_residual(1.9) is False
    assert material_residual(None) is False


def test_tier_full_on_material_residual_despite_flat_move() -> None:
    from worker.assemble import _tier

    # Tiny raw move, no RVOL spike, but a large residual → full tier.
    assert _tier(Fraction(1, 1000), None, resid_z=3.2) == "full"


def test_tier_unchanged_when_residual_immaterial() -> None:
    from worker.assemble import _tier

    assert _tier(Fraction(1, 1000), None, resid_z=0.5) == "suppressed"


def _ranked() -> BriefObject:
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40"), _lot("D", "5", "100")]
    prices = {
        "A": Price(c=Decimal("110"), prev_c=Decimal("100")),  # +10% → full
        "B": Price(c=Decimal("60"), prev_c=Decimal("50")),  # +20% → full
        "D": Price(c=Decimal("120"), prev_c=Decimal("100")),  # +20% → full
    }
    closes = {"A": Decimal("110"), "B": Decimal("60"), "D": Decimal("120")}
    decomp: dict[str, dict[str, object]] = {
        "A": {
            "market_bps": 50, "theme_bps": 10, "resid_bps": 40,
            "resid_z": 1.0, "provisional": False,
        },
        "B": {
            "market_bps": 100, "theme_bps": 50, "resid_bps": 850,
            "resid_z": -3.5, "provisional": True,
        },
        # D has no decomposition row yet → resid_z is None and sorts last.
    }
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT, decomp=decomp)


def test_attribution_rows_ranked_by_abs_resid_z_desc_none_last() -> None:
    obj = _ranked()
    assert obj.schema_version == 4
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    assert [r.symbol for r in attribution.rows] == ["B", "A", "D"]
    b_row = attribution.rows[0]
    assert b_row.resid_bps == 850
    assert b_row.resid_z == -3.5
    assert b_row.provisional is True
    d_row = attribution.rows[-1]
    assert d_row.resid_z is None


def test_row_accepts_decomposition_fields() -> None:
    from contracts.brief import Row
    r = Row.model_validate({
        "symbol": "SNDK", "tier": "full", "market_bps": 12, "theme_bps": -4,
        "resid_bps": 88, "resid_z": 3.1, "provisional": True,
    })
    assert r.resid_bps == 88 and r.resid_z == 3.1 and r.provisional is True


def test_row_without_decomposition_still_valid() -> None:
    from contracts.brief import Row
    r = Row.model_validate({"symbol": "MU", "tier": "brief"})  # v2-era row
    assert r.symbol == "MU"
