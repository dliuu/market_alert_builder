"""M19 technical snapshot: pure, no DB. Moving averages, ATR, volume ratios and
support/resistance zones from adjusted daily OHLCV.

The invariants that bite: the volume denominators exclude the measured session
(verify-numbers check 6, as in `tape.py`); every window yields `None` rather
than raising below its minimum observation count; and the zone merge is capped
so a chain of near-levels cannot collapse into one meaningless band.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from worker.technicals import (
    LOOKBACK_SESSIONS,
    MA_WINDOWS,
    MIN_SESSIONS,
    MIN_TOUCHES,
    Pivot,
    TechBar,
    cluster_zones,
    swing_pivots,
    technicals_for_symbol,
    to_price_space,
)

_START = date(2025, 1, 1)


def _d(x: str) -> Decimal:
    return Decimal(x)


def _bars(closes: list[str], *, volumes: list[int] | None = None) -> list[TechBar]:
    """Flat-range bars (h == l == c) at one close per session, oldest → newest."""
    vols = volumes if volumes is not None else [1000] * len(closes)
    return [
        TechBar(
            session_date=_START + timedelta(days=i),
            h=_d(c),
            l=_d(c),
            c=_d(c),
            v=v,
        )
        for i, (c, v) in enumerate(zip(closes, vols, strict=True))
    ]


# --- moving averages ------------------------------------------------------


def test_ma_is_the_mean_of_the_last_n_closes_including_today() -> None:
    # 20 closes of 100 then one of 120: the 20-day mean covers the last 20 bars,
    # which is nineteen 100s and today's 120.
    bars = _bars(["100"] * 20 + ["120"])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_20 == Decimal(1900 + 120) / Decimal(20)


def test_ma_null_one_observation_short_of_its_window() -> None:
    bars = _bars(["100"] * (20 - 1))
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_20 is None


def test_ma_uses_only_the_most_recent_window() -> None:
    # 40 bars; the older 20 are far away and must not move the 20-day mean.
    bars = _bars(["1000"] * 20 + ["100"] * 20)
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_20 == Decimal(100)


def test_longer_windows_stay_null_while_shorter_ones_resolve() -> None:
    bars = _bars(["100"] * 60)
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_20 == Decimal(100)
    assert t.ma_50 == Decimal(100)
    assert t.ma_200 is None


def test_ma_windows_are_20_50_200() -> None:
    assert MA_WINDOWS == (20, 50, 200)


# --- MA stack -------------------------------------------------------------


def test_ma_stack_bullish_when_short_above_long() -> None:
    # A steadily rising series puts 20 > 50 > 200.
    bars = _bars([str(200 + i) for i in range(200)])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_stack == "bullish"


def test_ma_stack_bearish_when_short_below_long() -> None:
    bars = _bars([str(600 - i) for i in range(200)])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_stack == "bearish"


def test_ma_stack_null_without_all_three_averages() -> None:
    bars = _bars(["100"] * 60)  # no 200-day yet
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.ma_stack is None


# --- volume vs its own averages -------------------------------------------


def test_volume_ratios_divide_today_by_the_prior_window_mean() -> None:
    # 21 prior sessions at 1000, then today at 2000.
    bars = _bars(["100"] * 22, volumes=[1000] * 21 + [2000])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.vol_vs_5d == Fraction(2)
    assert t.vol_vs_21d == Fraction(2)


def test_volume_denominators_exclude_today() -> None:
    # verify-numbers check 6. Today is enormous; were it in the denominator the
    # ratio would be pulled far below 5.
    bars = _bars(["100"] * 6, volumes=[100] * 5 + [500])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.vol_vs_5d == Fraction(5)


def test_volume_ratio_null_one_session_short_of_its_window() -> None:
    # 4 prior sessions + today = 5 bars; the 5-day window needs 5 prior.
    bars = _bars(["100"] * 5, volumes=[100] * 5)
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.vol_vs_5d is None


def test_volume_ratio_null_when_the_prior_window_is_all_zero() -> None:
    bars = _bars(["100"] * 6, volumes=[0] * 5 + [500])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.vol_vs_5d is None


def test_volume_ratio_uses_only_the_most_recent_prior_window() -> None:
    bars = _bars(["100"] * 12, volumes=[10_000] * 6 + [100] * 5 + [200])
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.vol_vs_5d == Fraction(2)


# --- ATR ------------------------------------------------------------------


def test_atr_is_the_mean_true_range_over_14_sessions() -> None:
    # Every bar spans 100-110 and closes at 110, so each true range is exactly
    # 10 except the first (no prior close, so it is excluded from the window).
    bars = [
        TechBar(
            session_date=_START + timedelta(days=i), h=_d("110"), l=_d("100"), c=_d("110"), v=1000
        )
        for i in range(20)
    ]
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.atr_14 == Fraction(10)


def test_atr_uses_the_gap_from_the_prior_close_when_it_is_wider() -> None:
    # A bar that gaps: prior close 100, today spans 120-125. The intraday range
    # is 5, but the true range measured from the prior close is 25.
    flat = [
        TechBar(
            session_date=_START + timedelta(days=i), h=_d("100"), l=_d("100"), c=_d("100"), v=1000
        )
        for i in range(14)
    ]
    gap = TechBar(
        session_date=_START + timedelta(days=14), h=_d("125"), l=_d("120"), c=_d("125"), v=1000
    )
    t = technicals_for_symbol("A", [*flat, gap], rvol=None)
    # Thirteen true ranges of 0 and one of 25, over the 14-session window.
    assert t.atr_14 == Fraction(25, 14)


def test_atr_null_without_enough_history() -> None:
    # 14 bars yield only 13 true ranges — the first bar has no prior close.
    bars = _bars(["100"] * 14)
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.atr_14 is None


# --- swing pivots ---------------------------------------------------------


def _ohlc(rows: list[tuple[str, str, str]], *, volumes: list[int] | None = None) -> list[TechBar]:
    """Bars from explicit (h, l, c) triples, oldest → newest."""
    vols = volumes if volumes is not None else [1000] * len(rows)
    return [
        TechBar(
            session_date=_START + timedelta(days=i),
            h=_d(row[0]),
            l=_d(row[1]),
            c=_d(row[2]),
            v=v,
        )
        for i, (row, v) in enumerate(zip(rows, vols, strict=True))
    ]


def test_pivot_high_is_the_strict_max_of_its_seven_bar_window() -> None:
    # A single peak at index 7, flanked by three lower bars either side.
    highs = [
        "100",
        "100",
        "100",
        "100",
        "100",
        "100",
        "100",
        "120",
        "100",
        "100",
        "100",
        "100",
        "100",
        "100",
        "100",
    ]
    bars = _ohlc([(h, "90", h) for h in highs])
    pivots = swing_pivots(bars)
    assert [(p.kind, p.price) for p in pivots if p.kind == "high"] == [("high", _d("120"))]


def test_pivot_low_is_the_strict_min_of_its_seven_bar_window() -> None:
    lows = [
        "90",
        "90",
        "90",
        "90",
        "90",
        "90",
        "90",
        "70",
        "90",
        "90",
        "90",
        "90",
        "90",
        "90",
        "90",
    ]
    bars = _ohlc([("100", low, "95") for low in lows])
    pivots = swing_pivots(bars)
    assert [(p.kind, p.price) for p in pivots if p.kind == "low"] == [("low", _d("70"))]


def test_the_last_k_bars_can_never_form_a_pivot() -> None:
    # The highest bar is the final one. It has no bars to its right, so it has
    # not held — a level is not a level until it survives k more sessions.
    highs = ["100"] * 14 + ["200"]
    bars = _ohlc([(h, "90", h) for h in highs])
    assert [p for p in swing_pivots(bars) if p.price == _d("200")] == []


def test_the_first_k_bars_can_never_form_a_pivot() -> None:
    highs = ["200"] + ["100"] * 14
    bars = _ohlc([(h, "90", h) for h in highs])
    assert [p for p in swing_pivots(bars) if p.price == _d("200")] == []


def test_a_plateau_is_not_a_strict_maximum() -> None:
    # Two adjacent equal highs: neither strictly dominates its window, so
    # neither is a pivot. Without this, a flat top emits two levels at one price.
    highs = ["100"] * 7 + ["120", "120"] + ["100"] * 7
    bars = _ohlc([(h, "90", h) for h in highs])
    assert [p for p in swing_pivots(bars) if p.price == _d("120")] == []


def test_pivot_carries_the_volume_of_the_bar_that_formed_it() -> None:
    highs = ["100"] * 7 + ["120"] + ["100"] * 7
    bars = _ohlc([(h, "90", h) for h in highs], volumes=[1000] * 7 + [9999] + [1000] * 7)
    peak = [p for p in swing_pivots(bars) if p.price == _d("120")]
    assert len(peak) == 1
    assert peak[0].v == 9999


# --- 52-week extremes -----------------------------------------------------


def test_52_week_extremes_span_the_full_lookback() -> None:
    closes = ["100"] * LOOKBACK_SESSIONS
    bars = _ohlc([(c, c, c) for c in closes])
    bars[10] = bars[10]._replace(h=_d("300"))
    bars[200] = bars[200]._replace(l=_d("40"))
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.high_52w == _d("300")
    assert t.low_52w == _d("40")


def test_52_week_extremes_null_below_a_full_year_of_sessions() -> None:
    # A field called "52-week high" that is really a 40-week high is a lie.
    bars = _ohlc([("100", "100", "100")] * (LOOKBACK_SESSIONS - 1))
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.high_52w is None
    assert t.low_52w is None


# --- zone clustering ------------------------------------------------------

_ATR = Fraction(10)  # merge radius 5, max zone width 10


def _pv(day: int, price: str, v: int = 1000) -> Pivot:
    return Pivot(_START + timedelta(days=day), _d(price), v, "high")


def _touch_bars(rows: list[tuple[int, str]]) -> list[TechBar]:
    """Bars whose high and low both sit at one price — a bar that touches
    exactly that level and nothing else."""
    return [
        TechBar(session_date=_START + timedelta(days=d), h=_d(p), l=_d(p), c=_d(p), v=1000)
        for d, p in rows
    ]


def test_levels_within_half_an_atr_merge_into_one_zone() -> None:
    pivots = [_pv(0, "100"), _pv(1, "104")]  # 4 apart, radius is 5
    zones = cluster_zones(pivots, _touch_bars([(0, "100"), (1, "104")]), _ATR)
    assert len(zones) == 1
    assert zones[0].price == Fraction(102)


def test_levels_beyond_half_an_atr_stay_separate() -> None:
    pivots = [_pv(0, "100"), _pv(1, "106")]  # 6 apart, radius is 5
    zones = cluster_zones(pivots, _touch_bars([(0, "100"), (1, "106")]), _ATR)
    assert [z.price for z in zones] == [Fraction(100), Fraction(106)]


def test_a_chain_of_near_levels_cannot_exceed_the_max_zone_width() -> None:
    # Each step is 4 — inside the merge radius — so a naive greedy merge would
    # swallow all four into one 12-wide band. The width cap must break the chain.
    pivots = [_pv(0, "100"), _pv(1, "104"), _pv(2, "108"), _pv(3, "112")]
    bars = _touch_bars([(0, "100"), (1, "104"), (2, "108"), (3, "112")])
    zones = cluster_zones(pivots, bars, _ATR)
    assert [z.price for z in zones] == [Fraction(104), Fraction(112)]


def test_zone_price_is_volume_weighted_across_its_members() -> None:
    # 100 traded 1000 shares, 104 traded 3000 — the zone sits nearer 104.
    pivots = [_pv(0, "100", 1000), _pv(1, "104", 3000)]
    zones = cluster_zones(pivots, _touch_bars([(0, "100"), (1, "104")]), _ATR)
    assert zones[0].price == Fraction(103)


def test_touches_count_visits_that_never_formed_a_pivot() -> None:
    # One pivot, but price came back to the zone twice more on its own. Those
    # returns are evidence; counting only pivots would understate the level.
    pivots = [_pv(0, "100")]
    bars = _touch_bars([(0, "100"), (1, "200"), (2, "101"), (3, "200"), (4, "99")])
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].touches == 3


def test_zone_records_the_most_recent_touch() -> None:
    pivots = [_pv(0, "100")]
    bars = _touch_bars([(0, "100"), (5, "101"), (2, "99")])
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].last_touch == _START + timedelta(days=5)


def test_no_zones_when_atr_is_zero() -> None:
    # A halted or perfectly flat series has no scale to measure proximity with.
    pivots = [_pv(0, "100")]
    assert cluster_zones(pivots, _touch_bars([(0, "100")]), Fraction(0)) == []


# --- support / resistance selection ---------------------------------------

# A saw-tooth that turns at 110 and 90, so pivot highs cluster at 110 and pivot
# lows at 90. Flat bars, so every true range is the 5-point step and ATR == 5.
_CYCLE = ["100", "105", "110", "105", "100", "95", "90", "95"]


def _sawtooth(n: int) -> list[TechBar]:
    return _ohlc([(_CYCLE[i % 8], _CYCLE[i % 8], _CYCLE[i % 8]) for i in range(n)])


def _append(bars: list[TechBar], price: str, *, v: int = 1000) -> list[TechBar]:
    nxt = bars[-1].session_date + timedelta(days=1)
    return [*bars, TechBar(session_date=nxt, h=_d(price), l=_d(price), c=_d(price), v=v)]


def test_support_is_the_nearest_zone_below_and_resistance_the_nearest_above() -> None:
    bars = _sawtooth(81)  # ends at 100, between the 90 and 110 zones
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.support is not None and t.resistance is not None
    assert t.support.price == Fraction(90)
    assert t.resistance.price == Fraction(110)


def test_a_reported_zone_carries_its_touch_count_and_last_touch() -> None:
    bars = _sawtooth(81)
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.resistance is not None
    assert t.resistance.touches >= MIN_TOUCHES
    assert t.resistance.last_touch <= bars[-1].session_date


def test_a_level_touched_only_once_is_not_reported() -> None:
    # An isolated spike to 150 forms a pivot, but nothing ever went back to it.
    # One touch is a coincidence, so there must be no resistance at all here.
    bars = _sawtooth(81)
    bars[40] = bars[40]._replace(h=_d("150"), l=_d("150"), c=_d("150"))
    bars = _append(bars, "120")
    t = technicals_for_symbol("A", bars, rvol=None)
    assert t.resistance is None
    assert t.support is not None and t.support.price == Fraction(110)


def test_levels_null_below_the_minimum_session_count() -> None:
    t = technicals_for_symbol("A", _sawtooth(MIN_SESSIONS - 1), rvol=None)
    assert t.support is None
    assert t.resistance is None


# --- breakout -------------------------------------------------------------


def test_breakout_up_when_price_closes_through_a_tested_zone_on_volume() -> None:
    bars = _append(_sawtooth(81), "115")  # prior close 100, through the 110 zone
    t = technicals_for_symbol("A", bars, rvol=Fraction(2))
    assert t.breakout == "up"


def test_breakout_down_when_price_closes_below_a_tested_zone_on_volume() -> None:
    # Ends at 95 on the way down; a close at 85 breaks the 90 zone.
    bars = _append(_sawtooth(80), "85")
    t = technicals_for_symbol("A", bars, rvol=Fraction(2))
    assert t.breakout == "down"


def test_no_breakout_without_a_zone_crossing() -> None:
    bars = _append(_sawtooth(81), "102")  # still between the zones
    t = technicals_for_symbol("A", bars, rvol=Fraction(2))
    assert t.breakout is None


def test_no_breakout_without_volume_confirmation() -> None:
    bars = _append(_sawtooth(81), "115")
    assert technicals_for_symbol("A", bars, rvol=Fraction(1)).breakout is None
    assert technicals_for_symbol("A", bars, rvol=None).breakout is None


def test_rvol_of_exactly_the_spike_threshold_does_not_confirm() -> None:
    # `_RVOL_SPIKE` is compared with a strict `>` in assemble.py; 1.5 exactly is
    # not a spike, and a breakout must not quietly use a looser rule.
    bars = _append(_sawtooth(81), "115")
    t = technicals_for_symbol("A", bars, rvol=Fraction(3, 2))
    assert t.breakout is None


# --- adjusted-space → price-space rescale ---------------------------------


def test_levels_rescale_from_adjusted_space_to_todays_prices() -> None:
    # The whole engine runs on the adjusted series, so its levels come out in
    # adjusted units. After a 2:1 split the adjusted close is half the real one,
    # and a level of 50 adjusted is a level of 100 on today's tape.
    bars = _append(_sawtooth(81), "100")
    t = technicals_for_symbol("A", bars, rvol=None)
    scaled = to_price_space(t, Fraction(2))

    assert t.resistance is not None and scaled.resistance is not None
    assert scaled.resistance.price == t.resistance.price * 2
    assert scaled.atr_14 == (t.atr_14 or Fraction(0)) * 2
    assert scaled.high_52w == t.high_52w  # still null below a full year


def test_rescaling_preserves_the_evidence_on_a_zone() -> None:
    bars = _sawtooth(81)
    t = technicals_for_symbol("A", bars, rvol=None)
    scaled = to_price_space(t, Fraction(2))
    assert t.resistance is not None and scaled.resistance is not None
    assert scaled.resistance.touches == t.resistance.touches
    assert scaled.resistance.last_touch == t.resistance.last_touch


def test_ratios_are_scale_invariant_and_must_not_be_rescaled() -> None:
    # Distances and volume ratios are dimensionless; multiplying them by the
    # split factor would be a silent, doubled lie.
    bars = _sawtooth(81)
    t = technicals_for_symbol("A", bars, rvol=None)
    scaled = to_price_space(t, Fraction(2))
    assert scaled.vol_vs_5d == t.vol_vs_5d
    assert scaled.ma_stack == t.ma_stack


def test_rescaling_by_one_is_the_identity() -> None:
    t = technicals_for_symbol("A", _sawtooth(81), rvol=None)
    assert to_price_space(t, Fraction(1)) == t


# --- what counts as a touch -----------------------------------------------


def test_a_bar_that_straddles_the_level_is_a_breach_not_a_test() -> None:
    # A wide bar whose range swallows the level never *tested* it — price went
    # straight through. Counting it inflates the evidence for a level that was
    # in fact ignored. (Found on real data: ASTS scored 61 touches in 276
    # sessions, because a whole-range predicate catches nearly every bar.)
    pivots = [_pv(0, "100")]
    bars = _touch_bars([(0, "100")]) + [
        TechBar(session_date=_START + timedelta(days=5), h=_d("120"), l=_d("80"),
                c=_d("110"), v=1000)
    ]
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].touches == 1


def test_consecutive_sessions_at_a_level_are_one_test() -> None:
    # Price resting on a level for three sessions visited it once. Counting
    # bars rather than visits makes a slow drift look like repeated defence.
    pivots = [_pv(0, "100")]
    bars = _touch_bars([(0, "100"), (1, "100"), (2, "100")])
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].touches == 1


def test_separated_visits_each_count() -> None:
    pivots = [_pv(0, "100")]
    bars = _touch_bars([(0, "100"), (1, "100"), (5, "200"), (9, "100"), (10, "100")])
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].touches == 2
    assert zones[0].last_touch == _START + timedelta(days=10)


def test_a_touch_needs_an_extreme_near_the_level() -> None:
    # The bar's high or low has to reach the level; being merely in the
    # neighbourhood mid-range is not a test.
    pivots = [_pv(0, "100")]
    near = TechBar(session_date=_START + timedelta(days=3), h=_d("102"), l=_d("99"),
                   c=_d("101"), v=1000)
    far = TechBar(session_date=_START + timedelta(days=6), h=_d("140"), l=_d("60"),
                  c=_d("100"), v=1000)
    away = _touch_bars([(1, "200")])
    bars = [*_touch_bars([(0, "100")]), *away, near, far]
    zones = cluster_zones(pivots, bars, _ATR)
    assert zones[0].touches == 2  # the pivot bar and `near`; `far` straddles
