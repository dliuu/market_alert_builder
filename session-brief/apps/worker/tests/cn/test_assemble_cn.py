"""CN close brief assembly: pure, no DB. Mirrors ``tests/test_assemble.py``'s
pattern — a hand-built three-name CN book (one full-tier mover, one brief-tier,
one suppressed), snapshotted against a frozen fixture (docs/04).

The book is fed straight into the shared, pure ``assemble()`` (``kind=
"close_cn"``, ``currency="CNY"``) exactly as ``assemble_cn_close_and_store``
does, minus the DB/compute_and_store plumbing that module adds."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from contracts.brief import BriefObject, Row
from worker.assemble import assemble, close_brief_should_skip
from worker.assemble_shared import to_contract_json
from worker.compute import ComputeResult, Lot, Price, compute
from worker.tape import TapeMetrics
from worker_cn.assemble import STALE_CN_BARS_SYNTHETIC

_SESSION = date(2026, 8, 14)  # a Friday
_USER = "00000000-0000-0000-0000-0000000000c1"
_GENERATED_AT = datetime(2026, 8, 14, 7, 20, 0, tzinfo=UTC)  # CN close + 20min, in UTC
_FIXTURE = Path(__file__).parent / "fixtures" / "cn_close_brief.json"

_FULL = "600519.SS"  # +10.0% -> full
_BRIEF = "601318.SS"  # -0.5% -> brief
_SUPPRESSED = "300750.SZ"  # +0.1% -> suppressed


def _tier_of(row: Row) -> str:
    assert row.tier is not None
    return row.tier.value


def _lot(symbol: str, shares: str, cost: str) -> Lot:
    cost_cents = int((Decimal(cost) * 100).to_integral_value())
    return Lot(symbol, Decimal(shares), cost_cents, date(2026, 8, 1))


def _tape(symbol: str, rvol: str | None, rp: str | None) -> TapeMetrics:
    return TapeMetrics(
        symbol=symbol,
        rvol=Fraction(rvol) if rvol is not None else None,
        range_position=Fraction(rp) if rp is not None else None,
    )


def _book() -> tuple[ComputeResult, dict[str, Decimal], dict[str, TapeMetrics]]:
    lots = [
        _lot(_FULL, "10", "1600"),
        _lot(_BRIEF, "20", "40"),
        _lot(_SUPPRESSED, "5", "300"),
    ]
    prices = {
        _FULL: Price(c=Decimal("1760"), prev_c=Decimal("1600")),  # +10.0%
        _BRIEF: Price(c=Decimal("39.80"), prev_c=Decimal("40")),  # -0.5%
        _SUPPRESSED: Price(c=Decimal("300.30"), prev_c=Decimal("300")),  # +0.1%
    }
    closes = {_FULL: Decimal("1760"), _BRIEF: Decimal("39.80"), _SUPPRESSED: Decimal("300.30")}
    tape = {
        _FULL: _tape(_FULL, "2", "0.8"),
        _BRIEF: _tape(_BRIEF, "1.2", "0.5"),
        _SUPPRESSED: _tape(_SUPPRESSED, "1", "0.5"),
    }
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))
    return result, closes, tape


def _cn_close() -> BriefObject:
    result, closes, tape = _book()
    return assemble(
        result,
        closes,
        tape,
        user_id=_USER,
        session_date=_SESSION,
        kind="close_cn",
        generated_at=_GENERATED_AT,
        currency="CNY",
        stale=[STALE_CN_BARS_SYNTHETIC],
    )


def test_kind_and_currency() -> None:
    obj = _cn_close()
    assert obj.kind.value == "close_cn"
    assert to_contract_json(obj)["currency"] == "CNY"


def test_subject_carries_cn_label_and_yuan_sign() -> None:
    obj = _cn_close()
    assert obj.subject.startswith("CN Close · ")
    assert "¥" in obj.subject


def test_contribution_bps_sums_to_book_day_bps_exactly() -> None:
    # Invariant 3 (verify-numbers), checked at the exact-Fraction stage rather
    # than on the rounded, tier-filtered attribution rows: the suppressed name's
    # contribution is still part of book.day_bps but never surfaces as a row, so
    # summing only the *shown* rows would not reconcile even within 1bp.
    result, _closes, _tape = _book()
    total_contribution = sum(
        (p.contribution_bps for p in result.positions if p.contribution_bps is not None),
        start=Fraction(0),
    )
    assert total_contribution == result.book.day_bps


def test_tiers_partition_every_name() -> None:
    obj = _cn_close()
    attribution = next(s for s in obj.sections if s.id.value == "attribution")
    tiers = {r.symbol: _tier_of(r) for r in attribution.rows}
    assert tiers == {_FULL: "full", _BRIEF: "brief"}
    assert obj.suppressed == [_SUPPRESSED]


def test_tape_quality_covers_every_owned_name() -> None:
    # §4 stopped tiering in M19 — it answers "where does each position stand",
    # which a suppressed name has an answer to. The CN book inherits that.
    obj = _cn_close()
    tape = next(s for s in obj.sections if s.id.value == "tape_quality")
    assert [r.symbol for r in tape.rows] == sorted([_FULL, _BRIEF, _SUPPRESSED])
    full = next(r for r in tape.rows if r.symbol == _FULL)
    assert full.rvol == 2.0
    assert full.range_position == 0.8


def test_cn_tape_quality_carries_no_levels() -> None:
    """CN bar history is still partly synthetic, so `worker_cn.assemble` passes
    no technicals and every level field stays null. A level computed off a
    synthetic bar would be confidently wrong rather than absent."""
    obj = _cn_close()
    tape = next(s for s in obj.sections if s.id.value == "tape_quality")
    assert all(r.support is None and r.resistance is None for r in tape.rows)
    assert all(r.ma50_dist is None and r.breakout is None for r in tape.rows)


def test_data_quality_discloses_synthetic_cn_bars() -> None:
    obj = _cn_close()
    assert STALE_CN_BARS_SYNTHETIC in obj.data_quality.stale


def test_no_catalysts_section_on_close_cn() -> None:
    # Catalysts is `close`-only (worker/assemble.py); close_cn never carries it.
    obj = _cn_close()
    assert all(s.id.value != "catalysts" for s in obj.sections)


def test_full_mover_session_sends() -> None:
    assert close_brief_should_skip(_cn_close()) is False


def test_matches_frozen_fixture() -> None:
    got = to_contract_json(_cn_close())
    expected = json.loads(_FIXTURE.read_text())
    assert got == expected
