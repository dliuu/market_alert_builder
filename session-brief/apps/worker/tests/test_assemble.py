"""Stage ④ assemble: pure, no DB. Explicit figure checks plus a snapshot of the
whole object against a frozen fixture (docs/04 — "freeze one session, assert the
object"). The input is the two-position book from test_compute, whose figures are
hand-verifiable, so the fixture is auditable rather than a mystery blob."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from worker.assemble import SCHEMA_VERSION, assemble
from worker.compute import Lot, Price, compute

_SESSION = date(2026, 8, 11)  # a Tuesday
_USER = "00000000-0000-0000-0000-000000000001"
_GENERATED_AT = datetime(2026, 8, 11, 20, 42, 11, tzinfo=UTC)
_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief.json"


def _fixture_input() -> tuple[list[Lot], dict[str, Price], dict[str, Decimal]]:
    lots = [
        Lot("A", Decimal("10"), 9000, date(2026, 8, 1)),  # 10 sh @ $90
        Lot("B", Decimal("20"), 4000, date(2026, 8, 1)),  # 20 sh @ $40
    ]
    prices = {
        "A": Price(c=Decimal("110"), prev_c=Decimal("100")),
        "B": Price(c=Decimal("48"), prev_c=Decimal("50")),
    }
    closes = {"A": Decimal("110"), "B": Decimal("48")}
    return lots, prices, closes


def _build() -> object:
    lots, prices, closes = _fixture_input()
    result = compute(_SESSION, lots, prices, benchmark_return=Fraction(1, 100))  # SPY +1%
    return assemble(
        result,
        closes,
        user_id=_USER,
        session_date=_SESSION,
        kind="close",
        generated_at=_GENERATED_AT,
    )


# --- Explicit figures (the spec, hand-checked) ---------------------------


def test_book_totals() -> None:
    book = _build().book
    assert book.value_cents == 206_000  # 10*$110 + 20*$48 = $2,060
    assert book.day_pnl_cents == 6_000  # 10*(+$10) + 20*(-$2) = +$60
    assert book.day_bps == 300  # $60 / $2,000 prior = 3.00% = 300 bps
    assert book.total_pnl_cents == 36_000  # 10*$20 + 20*$8 = $360
    assert book.vs_spy_bps == 200  # 300 bps book − 100 bps SPY
    assert book.total_pct == 36_000 / 170_000  # $360 / $1,700 cost


def test_attribution_rows() -> None:
    sections = _build().sections
    assert [s.id.value for s in sections] == ["attribution"]
    rows = {r.symbol: r for r in sections[0].rows}
    assert rows["A"].close == 110.0
    assert rows["A"].day_return == 0.1
    assert rows["A"].day_pnl_cents == 10_000
    assert rows["A"].contribution_bps == 500  # $100 / $2,000 prior = 500 bps
    assert rows["B"].contribution_bps == -200  # -$40 / $2,000 prior = -200 bps
    assert rows["B"].day_return == -0.04


def test_subject_is_numeric_and_deterministic() -> None:
    assert _build().subject == "Close · Tue Aug 11 — book +3.0% (+$60.00)"


def test_money_fields_are_integers() -> None:
    obj = _build()
    assert isinstance(obj.book.value_cents, int)
    assert isinstance(obj.book.day_pnl_cents, int)
    for row in obj.sections[0].rows:
        assert row.day_pnl_cents is None or isinstance(row.day_pnl_cents, int)
        assert row.total_pnl_cents is None or isinstance(row.total_pnl_cents, int)


def test_downstream_stages_are_empty_in_m4() -> None:
    obj = _build()
    assert obj.one_thing is None  # narration → M8
    assert obj.flags == []  # M7
    assert obj.claims == [] and obj.resolved_claims == []  # M6
    assert obj.suppressed == []  # M5
    assert obj.schema_version == SCHEMA_VERSION


def test_identifiers() -> None:
    obj = _build()
    assert obj.brief_id == f"{_USER}-2026-08-11-close"
    assert obj.generated_at == _GENERATED_AT


# --- Snapshot: the whole object against a frozen fixture ------------------


def test_matches_frozen_fixture() -> None:
    got = _build().model_dump(mode="json")
    expected = json.loads(_FIXTURE.read_text())
    assert got == expected
