from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

from worker.options import Contract
from worker.volatility import (
    StripRow,
    iv30_from_chain,
    realized_vol,
    regime,
    select_strip,
    selected_contracts,
    strip_rows,
    term_slope,
)

SESSION = date(2026, 10, 20)


def _contract(
    expiry: date,
    strike: str,
    right: str = "C",
    *,
    bid: str = "1.00",
    ask: str = "1.10",
    iv: str | None = "30",
) -> Contract:
    k = Decimal(strike)
    occ = f"TEST{expiry:%y%m%d}{right}{int(k * 1000):08d}"
    return Contract(
        symbol=occ,
        expiry=expiry,
        right=right,
        strike=k,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=1,
        ask_size=1,
        iv=Decimal(iv) if iv is not None else None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
        open_interest=0,
        volume=0,
        last=None,
        prev_day_close=None,
        percent_change=None,
    )


def _grid(expiry: date, spot: Decimal, *, iv: str = "30", step: str = "0.025") -> list[Contract]:
    """Calls and puts every 2.5% of spot from -20% to +20%, all two-sided."""
    out: list[Contract] = []
    pct = Decimal(step)
    for i in range(-8, 9):
        strike = (spot * (1 + pct * i)).quantize(Decimal("0.01"))
        for right in ("C", "P"):
            out.append(_contract(expiry, str(strike), right, iv=iv))
    return out


# --- select_strip ------------------------------------------------------------


def test_strip_picks_nearest_expiry_and_strike_for_every_cell() -> None:
    spot = Decimal("100")
    chain: list[Contract] = []
    # Listed expiries at 6, 15, 22 and 29 days: each tenor has a distinct nearest.
    for days in (6, 15, 22, 29):
        chain += _grid(SESSION + timedelta(days=days), spot)
    cells = select_strip(chain, spot, SESSION)
    assert len(cells) == 12
    assert [(c.tenor, c.rule) for c in cells][:4] == [
        (7, "itm10"),
        (7, "atm"),
        (7, "otm10"),
        (14, "itm10"),
    ]
    by_key = {(c.tenor, c.rule): c for c in cells}
    assert by_key[(7, "atm")].dte == 6
    assert by_key[(14, "atm")].dte == 15
    assert by_key[(21, "atm")].dte == 22
    assert by_key[(30, "atm")].dte == 29
    itm = by_key[(30, "itm10")]
    assert itm.contract is not None and itm.contract.strike == Decimal("90.00")
    assert itm.moneyness == Decimal("-0.1")
    assert itm.time_value_pct is not None and itm.time_value_pct < itm.premium_pct  # type: ignore[operator]
    otm = by_key[(30, "otm10")]
    assert otm.contract is not None and otm.contract.strike == Decimal("110.00")
    assert otm.time_value_pct == otm.premium_pct  # no intrinsic value
    assert not any(c.target_miss or c.tenor_collapsed for c in cells)


def test_strip_marks_collapsed_tenors_and_strike_misses() -> None:
    spot = Decimal("20.5")
    # One monthly expiry only, strikes every $2.50 (about 12% of spot).
    expiry = SESSION + timedelta(days=24)
    chain = [_contract(expiry, k) for k in ("15", "17.5", "20", "22.5")]
    cells = select_strip(chain, spot, SESSION)
    by_key = {(c.tenor, c.rule): c for c in cells}
    assert not by_key[(7, "atm")].tenor_collapsed
    assert all(by_key[(t, "atm")].tenor_collapsed for t in (14, 21, 30))
    itm = by_key[(7, "itm10")]
    assert itm.contract is not None and itm.contract.strike == Decimal("17.5")
    assert itm.target_miss  # -14.6% against a -10% target
    atm = by_key[(7, "atm")]
    assert atm.contract is not None and atm.contract.strike == Decimal("20")
    assert not atm.target_miss  # -2.4%, inside the 2.5% tolerance


def test_strip_with_monthlies_only_collapses_the_middle_tenors() -> None:
    """Review focus 1: monthlies at 24 and 52 days. Tenors 14 and 21 both
    resolve to the 24-day expiry, which tenor 7 claimed first. (Tenor 30 also
    resolves to 24, 6 days away against 22, so it is collapsed too.)"""
    spot = Decimal("100")
    chain = _grid(SESSION + timedelta(days=24), spot) + _grid(SESSION + timedelta(days=52), spot)
    cells = select_strip(chain, spot, SESSION)
    atm = {c.tenor: c for c in cells if c.rule == "atm"}
    assert atm[14].dte == 24 and atm[21].dte == 24
    assert atm[14].tenor_collapsed and atm[21].tenor_collapsed
    assert not atm[7].tenor_collapsed


def test_strip_ignores_one_sided_quotes_puts_and_same_day_expiries() -> None:
    spot = Decimal("100")
    expiry = SESSION + timedelta(days=7)
    chain = [
        _contract(expiry, "100", bid="0", ask="1.00"),  # no bid
        _contract(expiry, "100", "P"),  # a put
        _contract(SESSION, "100"),  # expires today
        _contract(expiry, "105"),  # the only usable call
    ]
    cells = select_strip(chain, spot, SESSION)
    chosen = {c.contract.symbol for c in cells if c.contract is not None}
    assert chosen == {_contract(expiry, "105").symbol}


def test_strip_with_no_usable_calls_is_twelve_empty_cells() -> None:
    cells = select_strip([], Decimal("100"), SESSION)
    assert len(cells) == 12 and all(c.contract is None for c in cells)


# --- iv30 --------------------------------------------------------------------


def test_iv30_interpolates_in_variance_between_the_bracketing_expiries() -> None:
    spot = Decimal("100")
    near = SESSION + timedelta(days=20)
    far = SESSION + timedelta(days=40)
    chain = _grid(near, spot, iv="20") + _grid(far, spot, iv="40")
    # A 5-day expiry is below the 7-day floor and ignored.
    chain += _grid(SESSION + timedelta(days=5), spot, iv="99")
    result = iv30_from_chain(chain, spot, SESSION)
    assert result.near is not None and result.near.dte == 20 and result.near.iv == Decimal("20")
    assert result.far is not None and result.far.dte == 40 and result.far.iv == Decimal("40")
    # var: 400*20=8000 and 1600*40=64000 → at 30: 36000 → iv = sqrt(1200) ≈ 34.64
    assert result.iv30 is not None
    assert result.iv30.quantize(Decimal("0.01")) == Decimal("34.64")
    assert result.n == 16  # 4 strikes × call+put × 2 expiries
    assert len(result.contributors) == 16 and len(set(result.contributors)) == 16
    assert result.dropped == ()


def test_iv30_drops_a_strike_whose_call_and_put_disagree() -> None:
    spot = Decimal("100")
    expiry = SESSION + timedelta(days=30)
    chain = _grid(expiry, spot, iv="30")
    stale = _contract(expiry, "102.50", "P", iv="45")  # put IV 15 points off its call
    chain = [c for c in chain if not (c.strike == Decimal("102.50") and c.right == "P")] + [stale]
    result = iv30_from_chain(chain, spot, SESSION)
    assert result.dropped == ("2026-11-19 102.50",)
    assert result.iv30 == Decimal("30")
    assert result.n == 6  # 4 strikes minus the dropped pair
    assert stale.symbol not in result.contributors


def test_iv30_parity_tolerance_scales_with_the_level_of_vol() -> None:
    """A 5-point call/put gap is a stale print on a 30-vol name and noise on a
    95-vol one: the tolerance is the larger of 3 points and 10% of the mean."""
    spot = Decimal("100")
    expiry = SESSION + timedelta(days=30)
    chain = _grid(expiry, spot, iv="95")
    noisy = _contract(expiry, "102.50", "P", iv="100")  # 5 points off, 5% of ~97
    chain = [c for c in chain if not (c.strike == Decimal("102.50") and c.right == "P")] + [noisy]
    result = iv30_from_chain(chain, spot, SESSION)
    assert result.dropped == ()
    assert result.n == 8


def test_iv30_is_none_when_nothing_is_eligible() -> None:
    result = iv30_from_chain([], Decimal("100"), SESSION)
    assert result.iv30 is None and result.n == 0 and result.contributors == ()


def test_iv30_is_none_when_every_strike_fails_parity() -> None:
    """Review focus 2: call and put IVs 20 points apart at every strike."""
    spot = Decimal("100")
    expiry = SESSION + timedelta(days=30)
    chain = [
        _contract(expiry, str(c.strike), c.right, iv="30" if c.right == "C" else "50")
        for c in _grid(expiry, spot)
    ]
    result = iv30_from_chain(chain, spot, SESSION)
    assert result.iv30 is None and result.n == 0
    assert len(result.dropped) == 4


# --- selected_contracts ------------------------------------------------------


def test_selected_contracts_unions_strip_and_iv30_sorted_and_deduped() -> None:
    spot = Decimal("100")
    chain = _grid(SESSION + timedelta(days=20), spot) + _grid(SESSION + timedelta(days=40), spot)
    picked = selected_contracts(chain, spot, SESSION)
    symbols = [c.symbol for c in picked]
    assert symbols == sorted(set(symbols))
    wanted = set(iv30_from_chain(chain, spot, SESSION).contributors)
    wanted |= {c.contract.symbol for c in select_strip(chain, spot, SESSION) if c.contract}
    assert set(symbols) == wanted


# --- realized_vol ------------------------------------------------------------


def test_realized_vol_of_a_constant_series_is_zero() -> None:
    assert realized_vol([Decimal(50)] * 25, 20) == Decimal(0)


def test_realized_vol_needs_window_plus_two_closes() -> None:
    closes = [Decimal(100) + Decimal(i % 3) for i in range(22)]
    assert realized_vol(closes, 20) is not None
    assert realized_vol(closes[:-1], 20) is None  # 21 closes: today excluded leaves 19 returns
    assert realized_vol(closes, 60) is None


def test_realized_vol_matches_a_hand_computed_series_and_excludes_today() -> None:
    raw = [100, 101, 99, 102, 103, 101, 500]  # 500 is today and must not count
    closes = [Decimal(x) for x in raw]
    rets = [math.log(raw[i + 1] / raw[i]) for i in range(5)]  # window=5 ending at 101
    mean = sum(rets) / 5
    stdev = math.sqrt(sum((r - mean) ** 2 for r in rets) / 4)
    expected = stdev * math.sqrt(252) * 100
    got = realized_vol(closes, 5)
    assert got is not None
    assert abs(float(got) - expected) < 1e-6


# --- term_slope --------------------------------------------------------------


def test_term_slope_is_short_atm_iv_minus_long_atm_iv() -> None:
    spot = Decimal("100")
    chain = _grid(SESSION + timedelta(days=7), spot, iv="40") + _grid(
        SESSION + timedelta(days=30), spot, iv="30"
    )
    assert term_slope(select_strip(chain, spot, SESSION)) == Decimal("10")


def test_term_slope_is_none_when_the_ends_collapse_onto_one_expiry() -> None:
    spot = Decimal("100")
    chain = _grid(SESSION + timedelta(days=24), spot)
    assert term_slope(select_strip(chain, spot, SESSION)) is None
    assert term_slope(select_strip([], spot, SESSION)) is None


# --- regime ------------------------------------------------------------------


def _regime(
    pctile: str | None,
    spread: str | None,
    *,
    thin: bool = False,
    ratio: str | None = None,
    median: str | None = None,
) -> str:
    def d(v: str | None) -> Decimal | None:
        return Decimal(v) if v is not None else None

    return regime(
        iv30_pctile=d(pctile),
        iv_hv_spread=d(spread),
        iv30_thin=thin,
        iv_vix_ratio=d(ratio),
        book_median_ratio=d(median),
    )


def test_regime_rich_on_a_high_percentile_over_realized() -> None:
    assert _regime("80", "5") == "rich"


def test_regime_rich_without_a_percentile_falls_back_to_the_book_median_ratio() -> None:
    assert _regime(None, "5", ratio="1.9", median="1.5") == "rich"
    assert _regime(None, "5", ratio="1.2", median="1.5") == "fair"


def test_regime_cheap_below_realized_or_on_a_low_percentile() -> None:
    assert _regime("50", "-2") == "cheap"
    assert _regime("20", "5") == "cheap"


def test_regime_is_fair_when_thin_whatever_else() -> None:
    assert _regime("80", "5", thin=True) == "fair"
    assert _regime("20", "-5", thin=True) == "fair"
    assert _regime(None, "5", thin=True, ratio="1.9", median="1.5") == "fair"


def test_regime_is_fair_without_a_spread() -> None:
    assert _regime("80", None) == "fair"
    assert _regime("20", None) == "fair"


# --- strip_rows --------------------------------------------------------------


def _prior_row(tenor: int, rule: str, premium_pct: str, iv: str) -> StripRow:
    return StripRow(
        tenor=tenor,
        rule=rule,
        contract=None,
        dte=None,
        moneyness=None,
        mid=None,
        premium_pct=Decimal(premium_pct),
        time_value_pct=None,
        iv=Decimal(iv),
        delta=None,
        open_interest=None,
        volume=None,
        chg_contract_pct=None,
        mid_chg=None,
        chg_tenor_pct=None,
        iv_chg=None,
        tenor_collapsed=False,
        target_miss=False,
    )


def test_strip_rows_computes_the_three_changes_against_the_priors() -> None:
    spot = Decimal("100")
    chain = _grid(SESSION + timedelta(days=30), spot)
    cells = select_strip(chain, spot, SESSION)
    atm30 = next(c for c in cells if (c.tenor, c.rule) == (30, "atm"))
    assert atm30.contract is not None and atm30.premium_pct is not None
    prior = {(30, "atm"): _prior_row(30, "atm", "0.0084", "28")}
    prior_mids = {atm30.contract.symbol: Decimal("0.84")}
    rows = strip_rows(cells, prior, prior_mids)
    assert len(rows) == 12
    row = next(r for r in rows if (r.tenor, r.rule) == (30, "atm"))
    assert row.contract == atm30.contract.symbol
    assert row.mid == Decimal("1.05")
    assert row.chg_tenor_pct == atm30.premium_pct / Decimal("0.0084") - 1
    assert row.chg_contract_pct == Decimal("1.05") / Decimal("0.84") - 1
    assert row.iv_chg == Decimal("2")
    assert row.iv == Decimal("30")


def test_strip_rows_mid_chg_is_the_same_contract_dollar_change() -> None:
    spot = Decimal("100")
    cells = select_strip(_grid(SESSION + timedelta(days=30), spot), spot, SESSION)
    atm30 = next(c for c in cells if (c.tenor, c.rule) == (30, "atm"))
    assert atm30.contract is not None
    rows = strip_rows(cells, {}, {atm30.contract.symbol: Decimal("0.84")})
    row = next(r for r in rows if (r.tenor, r.rule) == (30, "atm"))
    assert row.mid_chg == Decimal("1.05") - Decimal("0.84")
    other = next(r for r in rows if (r.tenor, r.rule) == (30, "otm10"))
    assert other.mid_chg is None  # no prior quote for that contract


def test_strip_rows_leaves_the_changes_none_without_priors() -> None:
    spot = Decimal("100")
    cells = select_strip(_grid(SESSION + timedelta(days=30), spot), spot, SESSION)
    rows = strip_rows(cells, {}, {})
    assert len(rows) == 12
    assert all(r.premium_pct is not None for r in rows)
    assert all(
        r.chg_contract_pct is None and r.chg_tenor_pct is None and r.iv_chg is None for r in rows
    )


def test_strip_rows_carries_an_empty_cell_through() -> None:
    rows = strip_rows(select_strip([], Decimal("100"), SESSION), {}, {})
    assert len(rows) == 12
    assert all(r.contract is None and r.mid is None for r in rows)
