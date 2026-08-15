"""M18: normalizing SEC EDGAR companyfacts into `fundamentals`.

Pure — a stored payload in, typed rows out, no network. Shapes here are copied
from a live `companyfacts` response (AAPL, verified 2026-08-14), not invented:
duration facts really do mix 3/6/9/12-month spans in one concept, and `filed`
really does trail `end` by months.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from worker.fundamentals import normalize_companyfacts

_ACCN = "0000320193-26-000020"


def obs(**kw: Any) -> dict[str, Any]:
    base = {"accn": _ACCN, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-07-31"}
    return {**base, **kw}


def facts(**concepts: Any) -> dict[str, Any]:
    dei = {k: v for k, v in concepts.items() if k.startswith("Entity")}
    gaap = {k: v for k, v in concepts.items() if not k.startswith("Entity")}
    return {"cik": 320193, "entityName": "Test Co", "facts": {"dei": dei, "us-gaap": gaap}}


def usd(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"USD": observations}}


def shares(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"units": {"shares": observations}}


# --- Point in time: the decision the whole milestone rests on (D31) ---------


def test_as_of_is_the_filing_date_not_the_period_end() -> None:
    """A Q2 fact is not knowable on the last day of Q2. Keying on `end` would
    let a metric for session D read a number the market got at D+45."""
    payload = facts(NetIncomeLoss=usd([
        obs(start="2026-03-29", end="2026-06-27", val=29789000000),
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert rows[0].as_of == date(2026, 7, 31)
    assert rows[0].period_end == date(2026, 6, 27)


def test_public_float_carries_its_own_long_filing_lag() -> None:
    """Live AAPL: EntityPublicFloat end 2025-03-28, filed 2025-10-31 — seven
    months. Keyed on `end` this would be visible two quarters early."""
    payload = facts(EntityPublicFloat=usd([
        {"end": "2025-03-28", "val": 3253431000000, "accn": "a", "fy": 2025,
         "fp": "FY", "form": "10-K", "filed": "2025-10-31"},
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert rows[0].as_of == date(2025, 10, 31)
    assert rows[0].public_float_cents == 325343100000000


# --- Duration filtering ----------------------------------------------------


def test_year_to_date_durations_are_discarded() -> None:
    """One concept mixes 3/6/9/12-month spans (live AAPL: 208/34/36/60). A
    9-month YTD figure summed as if quarterly would triple-count."""
    payload = facts(NetIncomeLoss=usd([
        obs(start="2026-03-29", end="2026-06-27", val=100),   # 3mo — keep
        obs(start="2025-12-28", end="2026-06-27", val=250),   # 6mo — drop
        obs(start="2025-09-28", end="2026-06-27", val=400),   # 9mo — drop
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert [r.net_income_cents for r in rows] == [10000]


def test_an_annual_duration_is_kept_and_tagged_fy() -> None:
    payload = facts(NetIncomeLoss=usd([
        {"start": "2025-09-28", "end": "2026-09-26", "val": 100, "accn": "b",
         "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-10-30"},
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert [(r.fiscal_period, r.net_income_cents) for r in rows] == [("FY", 10000)]


# --- Money -----------------------------------------------------------------


def test_money_becomes_integer_cents_exactly() -> None:
    payload = facts(CashAndCashEquivalentsAtCarryingValue=usd([
        obs(end="2026-06-27", val=25565000000),
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert rows[0].cash_cents == 2556500000000


def test_a_fractional_dollar_value_does_not_go_through_float() -> None:
    payload = facts(CashAndCashEquivalentsAtCarryingValue=usd([
        obs(end="2026-06-27", val="0.07"),
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert rows[0].cash_cents == 7  # not 7.000000000000001 -> 7 by luck


def test_a_cash_burning_company_gets_a_positive_burn() -> None:
    """The column is `quarterly_burn_cents`, not operating cash flow, and M7's
    `runway_quarters` divides by it — bailing out when the mean is <= 0. Storing
    raw OCF inverts the whole flag: a real cash-burner (negative OCF) would
    return no runway, while a cash *generator* would get a meaningless one. So
    burn is negated OCF."""
    payload = facts(NetCashProvidedByUsedInOperatingActivities=usd([
        obs(start="2026-03-29", end="2026-06-27", val=-4200000),
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert rows[0].quarterly_burn_cents == 420000000


def test_a_cash_generating_company_gets_a_negative_burn() -> None:
    """Which is what makes `runway_quarters` correctly decline to report a
    runway for a company that isn't consuming cash."""
    from worker.flags import runway_quarters

    payload = facts(NetCashProvidedByUsedInOperatingActivities=usd([
        obs(start="2026-03-29", end="2026-06-27", val=4200000),
    ]))

    burn = normalize_companyfacts(payload, symbol="TEST")[0].quarterly_burn_cents

    assert burn == -420000000
    assert runway_quarters(1_000_000, [burn or 0]) is None


# --- Grouping and shape ----------------------------------------------------


def test_one_row_per_filing_gathers_that_filings_concepts() -> None:
    payload = facts(
        NetIncomeLoss=usd([obs(start="2026-03-29", end="2026-06-27", val=100)]),
        CashAndCashEquivalentsAtCarryingValue=usd([obs(end="2026-06-27", val=200)]),
        EntityCommonStockSharesOutstanding=shares([obs(end="2026-07-17", val=14594180000)]),
    )

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert len(rows) == 1
    r = rows[0]
    assert (r.net_income_cents, r.cash_cents, r.shares_out) == (10000, 20000, 14594180000)


def test_two_filings_produce_two_rows_ordered_by_filing_date() -> None:
    payload = facts(NetIncomeLoss=usd([
        obs(start="2026-03-29", end="2026-06-27", val=100),
        {"start": "2025-12-29", "end": "2026-03-28", "val": 90, "accn": "older",
         "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-05-01"},
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert [r.as_of for r in rows] == [date(2026, 5, 1), date(2026, 7, 31)]


def test_an_amendment_lands_as_its_own_row(  ) -> None:
    """A 10-K/A restating a period keeps the original row intact; the later
    filing simply wins for readers that take the newest as_of per period."""
    payload = facts(NetIncomeLoss=usd([
        {"start": "2025-09-28", "end": "2026-09-26", "val": 100, "accn": "orig",
         "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-10-30"},
        {"start": "2025-09-28", "end": "2026-09-26", "val": 111, "accn": "amend",
         "fy": 2026, "fp": "FY", "form": "10-K/A", "filed": "2026-12-01"},
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert [(r.as_of, r.net_income_cents) for r in rows] == [
        (date(2026, 10, 30), 10000),
        (date(2026, 12, 1), 11100),
    ]
    assert {r.period_end for r in rows} == {date(2026, 9, 26)}


def test_the_cik_rides_along_zero_padded() -> None:
    payload = facts(NetIncomeLoss=usd([obs(start="2026-03-29", end="2026-06-27", val=1)]))

    assert normalize_companyfacts(payload, symbol="TEST")[0].cik == "0000320193"


def test_shares_out_falls_back_to_the_us_gaap_concept() -> None:
    """Not every registrant tags the dei cover-page count."""
    payload = facts(CommonStockSharesOutstanding=shares([obs(end="2026-06-27", val=5000)]))

    assert normalize_companyfacts(payload, symbol="TEST")[0].shares_out == 5000


def test_the_dei_cover_count_wins_over_the_us_gaap_one() -> None:
    payload = facts(
        EntityCommonStockSharesOutstanding=shares([obs(end="2026-06-27", val=9999)]),
        CommonStockSharesOutstanding=shares([obs(end="2026-06-27", val=5000)]),
    )

    assert normalize_companyfacts(payload, symbol="TEST")[0].shares_out == 9999


def test_a_multi_class_registrant_leaves_shares_out_null_rather_than_guessing() -> None:
    """companyfacts drops dimensional facts, so a company reporting shares per
    share class (live: ASTS) exposes no total. A weighted-average count is a
    different measure and must not be silently substituted for one."""
    payload = facts(
        NetIncomeLoss=usd([obs(start="2026-03-29", end="2026-06-27", val=100)]),
        WeightedAverageNumberOfSharesOutstandingBasic=shares([
            obs(start="2026-03-29", end="2026-06-27", val=300000000),
        ]),
    )

    row = normalize_companyfacts(payload, symbol="TEST")[0]

    assert row.net_income_cents == 10000  # the filing is still stored
    assert row.shares_out is None         # but not with a substituted measure


def test_a_payload_with_no_concepts_we_track_yields_nothing() -> None:
    payload = facts(SomeOtherConcept=usd([obs(end="2026-06-27", val=1)]))

    assert normalize_companyfacts(payload, symbol="TEST") == []


def test_an_8k_earnings_release_is_not_treated_as_a_periodic_filing() -> None:
    """Live AAPL carries 22 NetIncomeLoss observations from 8-Ks. They restate
    figures the 10-Q also reports; taking both would double a quarter."""
    payload = facts(NetIncomeLoss=usd([
        obs(start="2026-03-29", end="2026-06-27", val=100),
        {"start": "2026-03-29", "end": "2026-06-27", "val": 100, "accn": "8k",
         "fy": 2026, "fp": "Q3", "form": "8-K", "filed": "2026-07-30"},
    ]))

    rows = normalize_companyfacts(payload, symbol="TEST")

    assert [r.as_of for r in rows] == [date(2026, 7, 31)]
