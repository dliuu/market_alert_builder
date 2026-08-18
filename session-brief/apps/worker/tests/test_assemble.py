"""Stage ④ assemble: pure, no DB. Figure checks on a hand-verifiable book, the
M5 tiering rules (full / brief / suppressed), and a snapshot of a mixed session
against a frozen fixture (docs/04)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from contracts.brief import BriefObject, Row, Section
from worker.assemble import SCHEMA_VERSION, assemble, close_brief_should_skip
from worker.assemble_shared import to_contract_json
from worker.catalysts import CatalystItem
from worker.compute import Lot, Price, compute
from worker.tape import TapeMetrics
from worker.technicals import Technicals, Zone


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


def _tech(symbol: str, **over: object) -> Technicals:
    base: dict[str, object] = {
        "symbol": symbol,
        "ma_20": Fraction(100), "ma_50": Fraction(100), "ma_200": Fraction(100),
        "ma_stack": "mixed",
        "vol_vs_5d": Fraction(2), "vol_vs_21d": Fraction(3, 2),
        "atr_14": Fraction(5),
        "high_52w": Fraction(150), "low_52w": Fraction(50),
        "support": Zone(Fraction(90), 4, date(2026, 6, 12)),
        "resistance": Zone(Fraction(120), 3, date(2026, 7, 1)),
        "breakout": None,
    }
    base.update(over)
    return Technicals(**base)  # type: ignore[arg-type]


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
    # C is deliberately a *small* position (~4.6%): since the weight floor, only a
    # name under _ALWAYS_SHOW_WEIGHT can be suppressed at all, so a fat quiet name
    # would no longer exercise the roll-up line this fixture exists to cover.
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40"), _lot("C", "1", "100")]
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
    technicals = {
        "A": _tech("A", breakout="up"),
        "B": _tech("B"),
        "C": _tech("C", ma_200=None, ma_stack=None, resistance=None),
    }
    return assemble(result, closes, tape, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT,
                    catalysts=_catalysts(), technicals=technicals)


def _catalysts() -> list[CatalystItem]:
    """A full-tier cluster and a condensed 144, so the frozen fixture actually
    exercises the M17 rendering path — an empty section would let the template
    and the 80KB size check regress unnoticed."""
    return [
        CatalystItem(
            source="insider", symbol="A", kind="cluster", ref_date=date(2026, 8, 11),
            severity=4, tier="full",
            detail={"insider_count": 3, "total_value_cents": 1_420_000_000},
        ),
        CatalystItem(
            source="proposed", symbol="B", kind="standard_144", ref_date=date(2026, 8, 10),
            severity=2, tier="brief",
            detail={"shares": "1200000", "pct_of_float": "0.0024"},
        ),
    ]


def test_tiers_partition_every_name() -> None:
    obj = _mixed()
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    tiers = {r.symbol: _tier_of(r) for r in attribution.rows}
    assert tiers == {"A": "full", "B": "brief"}
    assert obj.suppressed == ["C"]
    # No name is lost: shown ∪ suppressed == every held symbol.
    assert set(tiers) | set(obj.suppressed) == {"A", "B", "C"}


def _tape_section(obj: BriefObject) -> Section:
    return next(s for s in obj.sections if s.id.value == "tape_quality")


def _mixed_without_technicals() -> BriefObject:
    """The same session with no technicals at all — the state of a book whose
    backfill never went deep enough."""
    lots = [_lot("A", "10", "90"), _lot("B", "20", "40"), _lot("C", "5", "100")]
    prices = {
        "A": Price(c=Decimal("110"), prev_c=Decimal("100")),
        "B": Price(c=Decimal("49.75"), prev_c=Decimal("50")),
        "C": Price(c=Decimal("100.1"), prev_c=Decimal("100")),
    }
    closes = {"A": Decimal("110"), "B": Decimal("49.75"), "C": Decimal("100.1")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(result, closes, {"A": _tape("A", "2", "0.8")}, user_id=_USER,
                    session_date=_SESSION, kind="close", generated_at=_GENERATED_AT)


def test_tape_quality_covers_every_owned_name() -> None:
    # M19 changed this deliberately: §4 was full-tier movers only, but a
    # snapshot of "every owned stock" has to include the quiet ones — a name
    # sitting on its support is exactly the one that did not move today.
    tape = _tape_section(_mixed())
    assert [r.symbol for r in tape.rows] == ["A", "B", "C"]  # C is suppressed elsewhere
    assert tape.rows[0].rvol == 2.0
    assert tape.rows[0].range_position == 0.8


def test_tape_quality_carries_the_technical_snapshot() -> None:
    row = next(r for r in _tape_section(_mixed()).rows if r.symbol == "A")
    assert row.ma50_dist == 0.1  # close 110 against a 50-day of 100
    assert row.ma_stack is not None and row.ma_stack.value == "mixed"
    assert row.vol_vs_5d == 2.0
    assert row.vol_vs_21d == 1.5
    assert row.atr14 == 5.0
    assert row.support == 90.0
    assert row.support_touches == 4
    assert row.support_last_touch is not None
    assert row.support_last_touch.isoformat() == "2026-06-12"
    assert row.resistance == 120.0
    assert row.high_52w == 150.0
    assert row.breakout is not None and row.breakout.value == "up"


def test_tape_quality_nulls_the_fields_a_symbol_has_no_history_for() -> None:
    row = next(r for r in _tape_section(_mixed()).rows if r.symbol == "C")
    assert row.ma200_dist is None
    assert row.ma_stack is None
    assert row.resistance is None
    assert row.resistance_touches is None
    assert row.resistance_last_touch is None
    assert row.ma50_dist is not None  # the shorter averages still resolved


def test_tape_quality_rows_survive_a_symbol_missing_from_technicals() -> None:
    # A name backfilled too shallowly has no entry at all. It still gets a row,
    # with null technicals — dropping it would silently shorten a snapshot that
    # claims to cover the whole book.
    row = next(r for r in _tape_section(_mixed_without_technicals()).rows if r.symbol == "A")
    assert row.rvol == 2.0
    assert row.ma50_dist is None
    assert row.support is None


def test_mixed_session_sends() -> None:
    assert close_brief_should_skip(_mixed()) is False  # A is a full-tier mover


def test_matches_frozen_fixture() -> None:
    got = to_contract_json(_mixed())
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


def test_schema_version_is_eight() -> None:
    # v4 = M13's attribution decomposition; v5 = M15's §2/§3 row fields and the
    # horizon-0 morning claim; v6 = M17's catalysts section; v7 = CN-M1's
    # `open_cn`/`close_cn` kinds and optional `currency`; v8 = M19's §4
    # technical snapshot (docs/04).
    assert _mixed().schema_version == SCHEMA_VERSION == 8


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


def test_tier_brief_on_large_weight_despite_flat_move() -> None:
    from worker.assemble import _tier

    # A position you can't afford to not see: flat move, but >15% of the book.
    assert _tier(Fraction(1, 1000), None, weight=Fraction(20, 100)) == "brief"


def test_tier_weight_floor_is_strict_above_15_percent() -> None:
    from worker.assemble import _tier

    assert _tier(Fraction(1, 1000), None, weight=Fraction(15, 100)) == "suppressed"


def test_tier_large_weight_does_not_downgrade_a_mover() -> None:
    from worker.assemble import _tier

    # The floor only rescues from suppression; it never pulls a name down.
    assert _tier(Fraction(5, 100), None, weight=Fraction(40, 100)) == "full"


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
    assert obj.schema_version == SCHEMA_VERSION
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    assert [r.symbol for r in attribution.rows] == ["B", "A", "D"]
    b_row = attribution.rows[0]
    assert b_row.resid_bps == 850
    assert b_row.resid_z == -3.5
    assert b_row.provisional is True
    d_row = attribution.rows[-1]
    assert d_row.resid_z is None


def _salience() -> BriefObject:
    """The 2026-08-14 shape: the name that drove the book has no decomposition,
    and the one that does has an immaterial residual."""
    lots = [_lot("MOVER", "100", "10"), _lot("TINY", "10", "10"), _lot("IDIO", "10", "10")]
    prices = {
        "MOVER": Price(c=Decimal("12"), prev_c=Decimal("10")),      # +20% → ~1667 bps
        "TINY": Price(c=Decimal("10.5"), prev_c=Decimal("10")),     # +5%  → ~42 bps
        "IDIO": Price(c=Decimal("10.5"), prev_c=Decimal("10")),     # +5%  → ~42 bps
    }
    closes = {"MOVER": Decimal("12"), "TINY": Decimal("10.5"), "IDIO": Decimal("10.5")}
    decomp: dict[str, dict[str, object]] = {
        # Immaterial residual: a fitted beta, but nothing idiosyncratic to say.
        "TINY": {"market_bps": 30, "theme_bps": 10, "resid_bps": 2,
                 "resid_z": 1.0, "provisional": False},
        # Genuinely idiosyncratic, even though it barely moved the book.
        "IDIO": {"market_bps": 5, "theme_bps": 2, "resid_bps": 35,
                 "resid_z": 3.0, "provisional": False},
        # MOVER has no fit at all (no theme / not yet refitted) → resid_z is None.
    }
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                    kind="close", generated_at=_GENERATED_AT, decomp=decomp)


def test_undecomposed_row_ranks_on_contribution_not_last() -> None:
    """A name with no decomposition must not be buried behind a trivial one.

    The regression (2026-08-14): the key was ``(resid_z is not None, |resid_z|)``,
    so *any* fitted row outranked *every* unfitted one. INFQ drove +212 bps of a
    +192 bps day but had no theme, so the brief led with ASTS at -25 bps and an
    immaterial |resid_z| of 1.34. Undecomposed is a recurring state — a position
    opened before the weekly refit, an IPO, a name in no theme — so the ordering
    has to degrade to materiality rather than to last place.
    """
    attribution = next(s for s in _salience().sections if s.id.value == "attribution")
    assert [r.symbol for r in attribution.rows] == ["IDIO", "MOVER", "TINY"]


def test_material_residual_still_leads_over_a_bigger_contributor() -> None:
    """The documented promise survives: a material |resid_z| (>= 2.0) leads even
    on a small raw move, which is the whole point of the decomposition."""
    attribution = next(s for s in _salience().sections if s.id.value == "attribution")
    lead = attribution.rows[0]
    assert lead.symbol == "IDIO"
    assert lead.resid_z == 3.0
    # ...and it led despite contributing far less than MOVER.
    mover = next(r for r in attribution.rows if r.symbol == "MOVER")
    assert mover.contribution_bps is not None
    assert lead.contribution_bps is not None
    assert mover.contribution_bps > lead.contribution_bps


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


# --- CN-M1 (v7): open_cn/close_cn kinds, optional currency ----------------


def test_cn_kinds_accepted_and_labelled() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": Price(c=Decimal("110"), prev_c=Decimal("100"))}  # +10% -> full
    closes = {"A": Decimal("110")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    for kind, label in (("open_cn", "CN Open"), ("close_cn", "CN Close")):
        obj = assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                       kind=kind, generated_at=_GENERATED_AT, currency="CNY")
        assert obj.kind.value == kind
        assert obj.subject.startswith(f"{label} · ")


def test_unknown_kind_still_rejected() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": Price(c=Decimal("110"), prev_c=Decimal("100"))}
    closes = {"A": Decimal("110")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    with pytest.raises(ValueError, match="unknown brief kind"):
        assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                kind="bogus", generated_at=_GENERATED_AT)


def test_currency_omitted_from_payload_for_usd() -> None:
    # `currency` is unset (None) on a USD object, but codegen types it as
    # `Currency | None = None` (docs/04's Pydantic-vs-schema gap), so a plain
    # `model_dump` would emit `"currency": null` — a shape the schema's own
    # enum rejects. `to_contract_json` is the boundary that strips it; that's
    # what actually gets stored (`_store_brief`), so it's what this asserts.
    assert "currency" not in to_contract_json(_two())


def test_currency_emitted_for_non_usd() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": Price(c=Decimal("110"), prev_c=Decimal("100"))}
    closes = {"A": Decimal("110")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    obj = assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                   kind="close_cn", generated_at=_GENERATED_AT, currency="CNY")
    assert to_contract_json(obj)["currency"] == "CNY"


def test_cn_subject_uses_signed_money_for_cny() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": Price(c=Decimal("110"), prev_c=Decimal("100"))}
    closes = {"A": Decimal("110")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    obj = assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                   kind="close_cn", generated_at=_GENERATED_AT, currency="CNY")
    assert "¥" in obj.subject


def test_close_cn_shares_the_close_skip_gate() -> None:
    # Same quiet-session setup as `test_quiet_session_is_skipped`, but close_cn.
    lots = [_lot("B", "20", "40"), _lot("C", "5", "100")]
    prices = {
        "B": Price(c=Decimal("49.75"), prev_c=Decimal("50")),
        "C": Price(c=Decimal("100.1"), prev_c=Decimal("100")),
    }
    closes = {"B": Decimal("49.75"), "C": Decimal("100.1")}
    tape = {"B": _tape("B", "1.2", "0.5"), "C": _tape("C", "1", "0.5")}
    result = compute(_SESSION, lots, prices)
    obj = assemble(result, closes, tape, user_id=_USER, session_date=_SESSION,
                   kind="close_cn", generated_at=_GENERATED_AT, currency="CNY")
    assert close_brief_should_skip(obj) is True


def test_open_cn_is_not_gated_by_the_close_skip_check() -> None:
    lots = [_lot("A", "10", "90")]
    prices = {"A": Price(c=Decimal("110"), prev_c=Decimal("100"))}
    closes = {"A": Decimal("110")}
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    obj = assemble(result, closes, {}, user_id=_USER, session_date=_SESSION,
                   kind="open_cn", generated_at=_GENERATED_AT, currency="CNY")
    assert close_brief_should_skip(obj) is False
