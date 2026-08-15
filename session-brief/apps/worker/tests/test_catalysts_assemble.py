"""M17: the catalysts section inside the BriefObject.

Assembly owns the mapping from the module's internal 1-5 severity onto the
object's tier, and owns suppression — the renderer never decides (D16).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import cast

from contracts.brief import BriefObject
from contracts.brief import Id as SectionId
from worker.assemble import assemble
from worker.catalysts import CatalystItem
from worker.catalysts_section import catalysts_section
from worker.compute import Lot, Price, compute

_D = date(2026, 8, 13)
_USER = "00000000-0000-0000-0000-000000000001"


def rows_of(section: dict[str, object]) -> list[dict[str, object]]:
    """`catalysts_section` returns the loose dict shape assemble.py's other
    section builders use, so the rows need narrowing before indexing."""
    return cast("list[dict[str, object]]", section["rows"])


def _close_brief(*, catalysts: list[CatalystItem], kind: str = "close") -> BriefObject:
    lots = [Lot("SNDK", Decimal("10"), 9000, date(2026, 8, 1))]
    prices = {"SNDK": Price(c=Decimal("110"), prev_c=Decimal("100"))}
    result = compute(_D, lots, prices, benchmark_return=Fraction(1, 100))
    return assemble(
        result, {"SNDK": Decimal("110")}, {},
        user_id=_USER, session_date=_D, kind=kind,
        generated_at=datetime(2026, 8, 13, 20, 42, 11, tzinfo=UTC),
        catalysts=catalysts,
    )


def item(
    *,
    symbol: str = "SNDK",
    kind: str = "cluster",
    severity: int = 4,
    tier: str = "full",
    source: str = "insider",
    detail: dict[str, object] | None = None,
) -> CatalystItem:
    return CatalystItem(
        source=source, symbol=symbol, kind=kind, ref_date=_D,
        severity=severity, tier=tier,
        detail=detail if detail is not None else {"insider_count": 3,
                                                  "total_value_cents": 1_420_000_000},
    )


def test_an_empty_feed_suppresses_the_whole_section() -> None:
    """Not an empty header — the section reports absence as one roll-up line
    or renders nothing at all (the M5 suppression principle)."""
    section = catalysts_section([], held=["SNDK", "RKLB"])

    assert section["tier"] == "suppressed"
    assert rows_of(section) == []
    assert "SNDK" in str(section["note"]) and "RKLB" in str(section["note"])


def test_names_with_no_catalysts_become_one_roll_up_line() -> None:
    section = catalysts_section([item(symbol="SNDK")], held=["SNDK", "RKLB", "ASTS"])

    assert [r["symbol"] for r in rows_of(section)] == ["SNDK"]
    assert section["note"] == "No catalysts: ASTS, RKLB"


def test_a_cluster_row_carries_its_figures_not_prose() -> None:
    section = catalysts_section([item()], held=["SNDK"])
    row = rows_of(section)[0]

    assert row["source"] == "insider"
    assert row["kind"] == "cluster"
    assert row["insider_count"] == 3
    assert row["value_cents"] == 1_420_000_000
    assert row["why"] is None  # narration fills this, and only this


def test_rows_are_ordered_by_severity_descending() -> None:
    section = catalysts_section(
        [
            item(symbol="RKLB", kind="standard_144", severity=2, tier="brief",
                 source="proposed", detail={"shares": 1200000, "pct_of_float": 0.0024}),
            item(symbol="SNDK", kind="clevel_buy", severity=5),
        ],
        held=["SNDK", "RKLB"],
    )

    assert [r["symbol"] for r in rows_of(section)] == ["SNDK", "RKLB"]


def test_a_decayed_tier_rides_on_the_row() -> None:
    section = catalysts_section([item(tier="brief")], held=["SNDK"])

    assert rows_of(section)[0]["tier"] == "brief"


def test_the_section_id_is_in_the_contract() -> None:
    section = catalysts_section([item()], held=["SNDK"])

    assert SectionId(section["id"]) is SectionId.catalysts


def test_a_decimal_from_the_detail_json_becomes_a_contract_number() -> None:
    """detail is jsonb, so it can hand back a string or a Decimal; the object
    must carry contract types, never a Decimal."""
    section = catalysts_section(
        [item(kind="large_144", source="proposed",
              detail={"shares": "600000", "pct_of_float": "0.006"})],
        held=["SNDK"],
    )
    row = rows_of(section)[0]

    assert row["shares"] == 600000.0
    assert row["pct_of_float"] == 0.006
    assert isinstance(row["pct_of_float"], float)


def test_the_close_brief_carries_the_catalysts_section() -> None:
    obj = _close_brief(catalysts=[item()])
    section = next(s for s in obj.sections if s.id is SectionId.catalysts)

    assert section.rows[0].symbol == "SNDK"
    assert section.rows[0].kind == "cluster"
    assert obj.schema_version == 6


def test_the_open_brief_has_no_catalysts_section() -> None:
    """Catalysts is post-close data (Form 4s land after the bell). The open
    brief is built by assemble_open and never sees it."""
    obj = _close_brief(catalysts=[item()], kind="open")

    assert all(s.id is not SectionId.catalysts for s in obj.sections)


def test_a_brief_with_no_catalysts_still_validates() -> None:
    obj = _close_brief(catalysts=[])
    section = next(s for s in obj.sections if s.id is SectionId.catalysts)

    assert section.tier.value == "suppressed"
    assert section.rows == []


def test_an_ambiguous_code_is_carried_onto_the_row() -> None:
    """Open question 2: the reader has to be able to see that the filter
    couldn't run, so the annotation cannot stop at the database."""
    section = catalysts_section(
        [item(kind="outsized_sale", severity=2, tier="brief",
              detail={"pct_of_holding": "0.45", "ambiguous_code": True})],
        held=["SNDK"],
    )

    assert rows_of(section)[0]["ambiguous_code"] is True
