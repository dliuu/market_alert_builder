"""M17 catalysts: the pure detector core.

Spec: docs/superpowers/specs/2026-08-14-m17-catalysts-design.md §5.1/§5.2.

No network, no database, no clock — a hand-built fixture per signal kind
asserting exact output, which is the milestone's golden-file DoD.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from worker.catalysts import (
    InsiderTx,
    ProposedSale,
    ReportingState,
    detect_insider,
    detect_proposed,
    render_tier,
)


def tx(
    *,
    name: str = "Jane Roe",
    title: str | None = None,
    code: str = "S",
    on: date = date(2026, 8, 13),
    shares: str = "1000",
    value_cents: int = 5_000_000,
    shares_after: str | None = None,
    row_id: int = 1,
) -> InsiderTx:
    return InsiderTx(
        symbol="SNDK",
        insider_name=name,
        insider_title=title,
        transaction_date=on,
        filing_date=on,
        transaction_code=code,
        shares=Decimal(shares),
        value_cents=value_cents,
        shares_after=Decimal(shares_after) if shares_after is not None else None,
        row_id=row_id,
    )


def kinds(signals: list[object]) -> set[str]:
    return {s.kind for s in signals}  # type: ignore[attr-defined]


def test_open_market_purchase_by_the_cfo_is_the_top_signal() -> None:
    signals = detect_insider([tx(name="John Doe", title="Chief Financial Officer", code="P")])

    assert [(s.kind, s.severity) for s in signals] == [("clevel_buy", 5)]
    assert signals[0].symbol == "SNDK"
    assert signals[0].member_ids == (1,)


def test_a_clevel_purchase_needs_to_be_a_purchase() -> None:
    signals = detect_insider([tx(title="Chief Executive Officer", code="S")])

    assert "clevel_buy" not in kinds(signals)


def test_three_insiders_selling_inside_five_sessions_is_a_cluster() -> None:
    signals = detect_insider([
        tx(name="A", on=date(2026, 8, 11), row_id=1),
        tx(name="B", on=date(2026, 8, 12), row_id=2),
        tx(name="C", on=date(2026, 8, 13), row_id=3),
    ])

    cluster = next(s for s in signals if s.kind == "cluster")
    assert cluster.severity == 4
    assert cluster.detail["insider_count"] == 3
    assert cluster.member_ids == (1, 2, 3)


def test_two_insiders_are_not_a_cluster() -> None:
    signals = detect_insider([
        tx(name="A", on=date(2026, 8, 12), row_id=1),
        tx(name="B", on=date(2026, 8, 13), row_id=2),
    ])

    assert "cluster" not in kinds(signals)


def test_one_insider_filing_three_times_is_not_a_cluster() -> None:
    """Three filings is not three insiders. The rule counts distinct people."""
    signals = detect_insider([
        tx(name="A", on=date(2026, 8, 11), row_id=1),
        tx(name="A", on=date(2026, 8, 12), row_id=2),
        tx(name="A", on=date(2026, 8, 13), row_id=3),
    ])

    assert "cluster" not in kinds(signals)


def test_buyers_and_sellers_do_not_combine_into_one_cluster() -> None:
    """'Same direction' — a split tape is not a cluster in either direction."""
    signals = detect_insider([
        tx(name="A", code="S", on=date(2026, 8, 11), row_id=1),
        tx(name="B", code="S", on=date(2026, 8, 12), row_id=2),
        tx(name="C", code="P", on=date(2026, 8, 13), row_id=3),
    ])

    assert "cluster" not in kinds(signals)


def test_three_sellers_spread_beyond_five_sessions_is_not_a_cluster() -> None:
    signals = detect_insider([
        tx(name="A", on=date(2026, 7, 1), row_id=1),
        tx(name="B", on=date(2026, 7, 20), row_id=2),
        tx(name="C", on=date(2026, 8, 13), row_id=3),
    ])

    assert "cluster" not in kinds(signals)


def test_selling_most_of_a_holding_is_an_outsized_sale() -> None:
    signals = detect_insider([tx(shares="4500", shares_after="5500")])

    outsized = next(s for s in signals if s.kind == "outsized_sale")
    assert outsized.severity == 3
    assert outsized.detail["pct_of_holding"] == Decimal("0.45")


def test_a_small_trim_is_not_an_outsized_sale() -> None:
    signals = detect_insider([tx(shares="100", shares_after="9900")])

    assert "outsized_sale" not in kinds(signals)


def test_an_outsized_sale_needs_a_known_prior_holding() -> None:
    """shares_after is nullable; without it the percentage is unknowable and
    the rule must not fire on an assumed denominator."""
    signals = detect_insider([tx(shares="4500", shares_after=None)])

    assert "outsized_sale" not in kinds(signals)


def test_a_sale_just_before_earnings_is_flagged() -> None:
    signals = detect_insider(
        [tx(on=date(2026, 8, 13))], earnings={"SNDK": date(2026, 8, 18)}
    )

    pre = next(s for s in signals if s.kind == "pre_earnings")
    assert pre.severity == 4
    assert pre.detail["days_to_event"] == 3


def test_a_sale_long_before_earnings_is_not_flagged() -> None:
    signals = detect_insider(
        [tx(on=date(2026, 8, 13))], earnings={"SNDK": date(2026, 11, 2)}
    )

    assert "pre_earnings" not in kinds(signals)


def test_pre_earnings_is_skipped_when_no_earnings_date_is_known() -> None:
    """docs/05: days_to_event is NULL and the rule is skipped — never inferred."""
    signals = detect_insider([tx(on=date(2026, 8, 13))], earnings={})

    assert "pre_earnings" not in kinds(signals)


def test_a_purchase_before_earnings_is_not_a_pre_earnings_sale() -> None:
    signals = detect_insider(
        [tx(on=date(2026, 8, 13), code="P")], earnings={"SNDK": date(2026, 8, 18)}
    )

    assert "pre_earnings" not in kinds(signals)


def test_a_regular_filer_doubling_their_usual_size_breaks_cadence() -> None:
    history = [
        tx(name="A", on=date(2026, m, 5), shares="1000", row_id=i)
        for i, m in enumerate((3, 4, 5, 6), start=1)
    ]
    signals = detect_insider([*history, tx(name="A", on=date(2026, 8, 13),
                                           shares="4000", row_id=9)])

    breaks = [s for s in signals if s.kind == "cadence_break"]
    assert [s.severity for s in breaks] == [3]
    assert breaks[0].member_ids == (9,)


def test_an_occasional_filer_is_not_held_to_a_cadence() -> None:
    """The rule needs >=4 prior filings before 'usual' means anything."""
    signals = detect_insider([
        tx(name="A", on=date(2026, 6, 5), shares="1000", row_id=1),
        tx(name="A", on=date(2026, 8, 13), shares="4000", row_id=2),
    ])

    assert "cadence_break" not in kinds(signals)


def test_tax_withholding_and_option_exercises_carry_no_signal() -> None:
    """Mechanical, non-discretionary dispositions. A CFO surrendering shares to
    cover withholding on a vest is not a CFO selling (§5.1)."""
    signals = detect_insider([
        tx(name="A", code="F", shares="4500", shares_after="5500", row_id=1),
        tx(name="B", code="M", shares="4500", shares_after="5500", row_id=2),
        tx(name="C", code="F", shares="4500", shares_after="5500", row_id=3),
    ])

    assert signals == []


def test_an_unmappable_vendor_code_degrades_severity_and_says_so() -> None:
    """Open question 2: if the vendor can't distinguish withholding, we neither
    silently include nor silently drop — severity drops by one and the item is
    annotated."""
    signals = detect_insider([tx(code="?", shares="4500", shares_after="5500")])

    outsized = next(s for s in signals if s.kind == "outsized_sale")
    assert outsized.severity == 2
    assert outsized.detail["ambiguous_code"] is True


# --- Form 144 / proposed sales (§5.2) --------------------------------------


def sale(
    *,
    name: str = "Jane Roe",
    on: date = date(2026, 8, 13),
    shares: str = "100000",
    row_id: int = 1,
) -> ProposedSale:
    return ProposedSale(
        symbol="SNDK",
        insider_name=name,
        filing_date=on,
        shares_proposed=Decimal(shares),
        approx_sale_date=None,
        row_id=row_id,
    )


def test_any_new_144_is_reported() -> None:
    signals = detect_proposed([sale()], as_of=date(2026, 8, 13))

    assert [(s.kind, s.severity) for s in signals] == [("standard_144", 2)]
    assert signals[0].source == "proposed"


def test_a_144_over_half_a_percent_of_float_is_a_large_144() -> None:
    signals = detect_proposed(
        [sale(shares="600000")],
        floats={"SNDK": Decimal("100000000")},
        as_of=date(2026, 8, 13),
    )

    assert [(s.kind, s.severity) for s in signals] == [("large_144", 4)]
    assert signals[0].detail["pct_of_float"] == Decimal("0.006")


def test_a_small_144_stays_standard_even_with_a_known_float() -> None:
    signals = detect_proposed(
        [sale(shares="100000")],
        floats={"SNDK": Decimal("100000000")},
        as_of=date(2026, 8, 13),
    )

    assert [s.kind for s in signals] == ["standard_144"]


def test_without_a_float_a_144_cannot_be_large() -> None:
    """Open question 4: pct_of_float is nullable and the magnitude rule must not
    fire on an assumed denominator. The filing is still reported."""
    signals = detect_proposed([sale(shares="600000")], floats={}, as_of=date(2026, 8, 13))

    assert [s.kind for s in signals] == ["standard_144"]
    assert signals[0].detail["pct_of_float"] is None


def test_a_144_with_no_matching_form_4_after_45_days_is_unconverted() -> None:
    signals = detect_proposed([sale(on=date(2026, 6, 1))], as_of=date(2026, 8, 13))

    unconverted = next(s for s in signals if s.kind == "unconverted_144")
    assert unconverted.severity == 3
    assert unconverted.detail["days_outstanding"] == 73


def test_a_144_the_insider_actually_executed_is_not_unconverted() -> None:
    signals = detect_proposed(
        [sale(name="Jane Roe", on=date(2026, 6, 1))],
        insider_txs=[tx(name="Jane Roe", code="S", on=date(2026, 6, 10))],
        as_of=date(2026, 8, 13),
    )

    assert "unconverted_144" not in kinds(signals)


def test_a_different_insiders_form_4_does_not_convert_this_144() -> None:
    signals = detect_proposed(
        [sale(name="Jane Roe", on=date(2026, 6, 1))],
        insider_txs=[tx(name="John Doe", code="S", on=date(2026, 6, 10))],
        as_of=date(2026, 8, 13),
    )

    assert "unconverted_144" in kinds(signals)


def test_a_form_4_before_the_144_does_not_convert_it() -> None:
    """The sale has to follow the filing to be the filing's execution."""
    signals = detect_proposed(
        [sale(name="Jane Roe", on=date(2026, 6, 1))],
        insider_txs=[tx(name="Jane Roe", code="S", on=date(2026, 5, 2))],
        as_of=date(2026, 8, 13),
    )

    assert "unconverted_144" in kinds(signals)


def test_a_recent_144_is_not_yet_unconverted() -> None:
    signals = detect_proposed([sale(on=date(2026, 8, 1))], as_of=date(2026, 8, 13))

    assert "unconverted_144" not in kinds(signals)


# --- Severity -> tier, and report-once decay (§6, §7) -----------------------


def test_severity_maps_onto_the_objects_three_tiers() -> None:
    """Severity 1-5 is internal; the BriefObject speaks full/brief/suppressed,
    and assembly does the mapping because the renderer never decides (D16)."""
    assert [render_tier(s, None) for s in (5, 4, 3, 2, 1)] == [
        "full", "full", "brief", "brief", None
    ]


def test_a_signal_seen_a_second_time_is_condensed() -> None:
    assert render_tier(5, ReportingState(report_count=1, max_severity_seen=5)) == "brief"


def test_a_signal_seen_a_third_time_is_suppressed() -> None:
    assert render_tier(5, ReportingState(report_count=2, max_severity_seen=5)) is None


def test_decay_never_promotes_a_low_severity_signal() -> None:
    """A severity-2 item opens condensed, not full — decay only demotes."""
    assert render_tier(2, None) == "brief"


def test_a_signal_that_gets_worse_re_escalates_to_full() -> None:
    """A 2-insider cluster becoming a 4-insider cluster is new information (§7)."""
    seen_twice = ReportingState(report_count=2, max_severity_seen=3)
    assert render_tier(3, seen_twice) is None
    assert render_tier(5, seen_twice) == "full"


def test_re_escalation_needs_the_severity_to_actually_rise() -> None:
    seen_twice = ReportingState(report_count=2, max_severity_seen=5)
    assert render_tier(5, seen_twice) is None
