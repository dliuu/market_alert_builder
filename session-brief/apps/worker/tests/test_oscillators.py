"""M20 pure indicator math. Known answers are hand-computed from the fixed
series below; they are asserted at three offsets rather than only the last bar,
because an EMA seeding bug shows up early and is masked by the tail."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from worker.oscillators import (
    ADX_WINDOW,
    MACD_SIGNAL,
    MACD_SLOW,
    RSI_WINDOW,
    adx_series,
    atr_series,
    macd_hist_series,
    rsi_series,
)
from worker.technicals import TechBar, _atr


def _closes(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


# A 40-close series with a clear up-leg, a pullback and a recovery, so RSI
# visits both extremes and MACD changes sign at least once.
SERIES = _closes(
    [
        "44.34", "44.09", "44.15", "43.61", "44.33", "44.83", "45.10", "45.42",
        "45.84", "46.08", "45.89", "46.03", "45.61", "46.28", "46.28", "46.00",
        "46.03", "46.41", "46.22", "45.64", "46.21", "46.25", "45.71", "46.45",
        "45.78", "45.35", "44.03", "44.18", "44.22", "44.57", "43.42", "42.66",
        "43.13", "43.85", "44.20", "44.90", "45.30", "45.10", "45.60", "46.10",
    ]
)


def _bars(closes: list[Decimal]) -> list[TechBar]:
    """Bars with a deterministic 1% range around each close, oldest → newest."""
    out = []
    day = date(2026, 1, 5)
    for i, c in enumerate(closes):
        span = (c * Decimal("0.005")).quantize(Decimal("0.01"))
        out.append(
            TechBar(
                session_date=day + timedelta(days=i),
                h=c + span,
                l=c - span,
                c=c,
                v=1_000_000 + i,
            )
        )
    return out


def test_rsi_first_value_is_at_the_window_boundary():
    rsi = rsi_series(SERIES)
    # 40 closes -> 39 deltas -> first Wilder average at delta 14 -> 26 values.
    assert len(rsi) == len(SERIES) - RSI_WINDOW
    assert all(Decimal(0) <= v <= Decimal(100) for v in rsi)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, "70.46"), (10, "54.67"), (-1, "60.22")],
)
def test_rsi_known_answers(offset: int, expected: str):
    """Wilder RSI(14), SMA-seeded, to 2dp at three separate offsets."""
    rsi = rsi_series(SERIES)
    assert rsi[offset].quantize(Decimal("0.01")) == Decimal(expected)


def test_rsi_all_gains_is_100():
    rising = _closes([str(Decimal(10) + Decimal(i)) for i in range(30)])
    assert rsi_series(rising)[-1] == Decimal(100)


def test_rsi_returns_empty_below_window():
    assert rsi_series(SERIES[:RSI_WINDOW]) == []


def test_macd_hist_first_value_is_at_the_warmup_boundary():
    hist = macd_hist_series(SERIES)
    warmup = MACD_SLOW + MACD_SIGNAL - 2  # 33
    assert len(hist) == len(SERIES) - warmup


@pytest.mark.parametrize("offset", [0, 3, -1])
def test_macd_hist_is_line_minus_signal(offset: int):
    """The histogram is definitionally line - signal; assert the identity holds
    at three offsets rather than trusting one endpoint."""
    from worker.oscillators import _ema, _macd_line

    line = _macd_line(SERIES)
    signal = _ema(line, MACD_SIGNAL)
    hist = macd_hist_series(SERIES)
    assert len(hist) == len(signal)
    aligned = line[len(line) - len(signal):]
    assert hist[offset] == aligned[offset] - signal[offset]


def test_macd_hist_returns_empty_below_warmup():
    assert macd_hist_series(SERIES[:33]) == []


def test_adx_first_value_is_at_the_warmup_boundary():
    adx = adx_series(_bars(SERIES))
    warmup = 2 * ADX_WINDOW - 1  # 27
    assert len(adx) == len(SERIES) - warmup
    assert all(Decimal(0) <= v <= Decimal(100) for v in adx)


def test_adx_inside_bar_contributes_no_directional_movement():
    """An inside bar (lower high AND higher low) must add zero to both +DM and
    -DM. A naive implementation credits one of them and skews +DI/-DI."""
    from worker.oscillators import _directional_movement

    prev = TechBar(date(2026, 1, 5), Decimal("50"), Decimal("45"), Decimal("48"), 1)
    inside = TechBar(date(2026, 1, 6), Decimal("49"), Decimal("46"), Decimal("47"), 1)
    assert _directional_movement(prev, inside) == (Decimal(0), Decimal(0))


def test_adx_outside_bar_credits_only_the_larger_move():
    from worker.oscillators import _directional_movement

    prev = TechBar(date(2026, 1, 5), Decimal("50"), Decimal("45"), Decimal("48"), 1)
    outside = TechBar(date(2026, 1, 6), Decimal("52"), Decimal("44"), Decimal("51"), 1)
    up, down = _directional_movement(prev, outside)
    assert (up, down) == (Decimal(2), Decimal(0))


def test_atr_series_last_value_equals_m19_atr():
    """The drift guard. technicals._atr is the certified M19 implementation and
    computes today's value only; this module needs the whole series for its
    percentile. Two implementations of one definition is a drift risk, closed
    here rather than by a comment."""
    from fractions import Fraction

    bars = _bars(SERIES)
    mine = Fraction(atr_series(bars)[-1])   # Decimal, quantized to 10dp
    m19 = _atr(bars)                        # exact Fraction
    assert m19 is not None
    # Not equality: this module quantizes to 10dp and technicals.py is exact.
    # The tolerance is one unit in the last quantized place.
    assert abs(mine - m19) < Fraction(1, 10**9)


def test_series_are_deterministic_across_prefixes():
    """A longer prefix of the same data must not change the overlapping values —
    the property that makes fixture snapshots stable."""
    full = rsi_series(SERIES)
    short = rsi_series(SERIES[:35])
    assert full[: len(short)] == short


from worker.oscillators import (
    BASELINE_SESSIONS,
    READ_DEPTH,
    REL_WINDOW,
    Standing,
    percentile,
    rel_strength_series,
    standing_for_symbol,
)


def _flat(n: int, value: str = "5") -> list[Decimal]:
    return [Decimal(value)] * n


def test_percentile_needs_a_full_baseline():
    """252 PRIOR values plus today = 253. One short yields None, not a
    percentile over a partial year."""
    assert percentile(_flat(BASELINE_SESSIONS)) is None
    assert percentile(_flat(BASELINE_SESSIONS + 1)) is not None


def test_monotone_series_puts_today_at_100():
    rising = [Decimal(i) for i in range(BASELINE_SESSIONS + 1)]
    assert percentile(rising) == Decimal(100)


def test_monotone_falling_series_puts_today_at_0():
    falling = [Decimal(-i) for i in range(BASELINE_SESSIONS + 1)]
    assert percentile(falling) == Decimal(0)


def test_ties_use_strict_less_than():
    """A flat history scores 0, not 50. A midpoint convention would invent
    movement on ADX, which sits on repeated values for long stretches."""
    assert percentile(_flat(BASELINE_SESSIONS + 1)) == Decimal(0)


def test_today_is_excluded_from_its_own_baseline():
    """The denominator rule, tested directly: appending today to the history it
    is ranked against must not change its percentile."""
    history = [Decimal(i) for i in range(BASELINE_SESSIONS)]
    outlier = Decimal(10_000)
    assert percentile([*history, outlier]) == Decimal(100)
    # An extra copy of the outlier inside the baseline DOES change the answer —
    # proving the baseline is the 252 values before today, not a window that
    # slid to include it.
    assert percentile([*history[1:], outlier, outlier]) < Decimal(100)


def test_percentile_uses_only_the_last_252_prior_values():
    ancient = [Decimal(10_000)] * 50
    recent = [Decimal(1)] * BASELINE_SESSIONS
    assert percentile([*ancient, *recent, Decimal(2)]) == Decimal(100)


def test_read_depth_covers_the_longest_warmup():
    """286 = 252 baseline + 1 today + 33 MACD warmup. Asserted directly so a
    later change to a warmup constant fails here rather than silently ranking
    against 251 observations."""
    assert READ_DEPTH == BASELINE_SESSIONS + 1 + (MACD_SLOW + MACD_SIGNAL - 2)


def test_rel_strength_is_the_return_difference():
    closes = _flat(REL_WINDOW + 1, "100")
    closes[-1] = Decimal("110")           # +10%
    bench = _flat(REL_WINDOW + 1, "50")
    bench[-1] = Decimal("52")             # +4%
    series = rel_strength_series(closes, bench)
    assert series[-1].quantize(Decimal("0.0001")) == Decimal("0.0600")


def test_rel_strength_empty_when_benchmark_is_short():
    assert rel_strength_series(_flat(60), _flat(10)) == []


def _long_bars(n: int) -> list[TechBar]:
    """n bars whose closes drift up with a sawtooth, so no indicator is flat."""
    closes = [Decimal(100) + Decimal(i) / Decimal(10) + (Decimal(i % 7) / Decimal(4)) for i in range(n)]
    return _bars([c.quantize(Decimal("0.01")) for c in closes])


def test_standing_with_full_history_populates_every_pair():
    bars = _long_bars(READ_DEPTH)
    s = standing_for_symbol("ASTS", bars, benchmark_bars=bars, benchmark_symbol="SPY")
    assert isinstance(s, Standing)
    for field in ("rsi14", "macd_hist", "adx14", "atr_pct", "rel_strength"):
        assert getattr(s, field) is not None, field
        assert getattr(s, f"{field}_pctile") is not None, field
    assert s.rel_strength_benchmark == "SPY"


def test_standing_with_short_history_keeps_values_and_nulls_percentiles():
    """Both halves asserted: a null percentile beside a populated value. Testing
    only the null would pass if the whole object went missing."""
    bars = _long_bars(200)
    s = standing_for_symbol("ASTS", bars, benchmark_bars=bars, benchmark_symbol="SPY")
    assert s.rsi14 is not None
    assert s.rsi14_pctile is None
    assert s.macd_hist is not None
    assert s.macd_hist_pctile is None


def test_standing_at_285_and_286_bars_is_the_percentile_boundary():
    short = standing_for_symbol("A", _long_bars(285), benchmark_bars=_long_bars(285), benchmark_symbol="SPY")
    exact = standing_for_symbol("A", _long_bars(286), benchmark_bars=_long_bars(286), benchmark_symbol="SPY")
    assert short.macd_hist_pctile is None
    assert exact.macd_hist_pctile is not None


def test_standing_without_a_benchmark_nulls_both_relative_fields():
    bars = _long_bars(READ_DEPTH)
    s = standing_for_symbol("ASTS", bars, benchmark_bars=[], benchmark_symbol=None)
    assert s.rel_strength is None
    assert s.rel_strength_pctile is None
    assert s.rel_strength_benchmark is None


from worker.oscillators import DIVERGENCE_MAX_GAP, DIVERGENCE_MIN_GAP, divergence


def _bars_from(pairs: list[tuple[str, str, str]]) -> list[TechBar]:
    """(high, low, close) triples -> bars on consecutive days."""
    day = date(2026, 1, 5)
    return [
        TechBar(day + timedelta(days=i), Decimal(h), Decimal(l), Decimal(c), 1_000_000)
        for i, (h, l, c) in enumerate(pairs)
    ]


def _peak_series(first_peak: str, second_peak: str, gap: int) -> list[TechBar]:
    """A trough-peak-trough-peak shape with two clean k=3 swing highs `gap`
    sessions apart, padded so both pivots are confirmable."""
    pairs: list[tuple[str, str, str]] = []

    def flat(n: int, level: str) -> None:
        for _ in range(n):
            pairs.append((level, level, level))

    flat(6, "100")
    pairs.append((first_peak, first_peak, first_peak))
    flat(gap - 1, "100")
    pairs.append((second_peak, second_peak, second_peak))
    flat(6, "100")
    return _bars_from(pairs)


def test_bearish_divergence_fires_on_higher_high_with_lower_rsi():
    bars = _peak_series("130", "135", 20)
    rsi = rsi_series([b.c for b in bars])
    # Force the second peak's RSI below the first's — the definition under test.
    assert divergence(bars, rsi, _rsi_at={6: Decimal("80"), 26: Decimal("60")}) == "bearish"


def test_higher_high_with_higher_rsi_fires_nothing():
    """The load-bearing negative. A detector that always fires passes the
    positive test above; only this one distinguishes it from a real one."""
    bars = _peak_series("130", "135", 20)
    rsi = rsi_series([b.c for b in bars])
    assert divergence(bars, rsi, _rsi_at={6: Decimal("60"), 26: Decimal("80")}) is None


def test_bullish_divergence_fires_on_lower_low_with_higher_rsi():
    pairs: list[tuple[str, str, str]] = []
    for _ in range(6):
        pairs.append(("100", "100", "100"))
    pairs.append(("70", "70", "70"))
    for _ in range(19):
        pairs.append(("100", "100", "100"))
    pairs.append(("65", "65", "65"))
    for _ in range(6):
        pairs.append(("100", "100", "100"))
    bars = _bars_from(pairs)
    rsi = rsi_series([b.c for b in bars])
    assert divergence(bars, rsi, _rsi_at={6: Decimal("20"), 26: Decimal("35")}) == "bullish"


def test_pivots_closer_than_the_minimum_gap_fire_nothing():
    bars = _peak_series("130", "135", DIVERGENCE_MIN_GAP - 2)
    rsi = rsi_series([b.c for b in bars])
    assert divergence(bars, rsi) is None


def test_pivots_further_than_the_maximum_gap_fire_nothing():
    bars = _peak_series("130", "135", DIVERGENCE_MAX_GAP + 5)
    rsi = rsi_series([b.c for b in bars])
    assert divergence(bars, rsi) is None


def test_fewer_than_two_pivots_fires_nothing():
    assert divergence(_bars_from([("100", "100", "100")] * 20), []) is None


def test_standing_carries_the_divergence():
    bars = _peak_series("130", "135", 20)
    s = standing_for_symbol("X", bars, benchmark_bars=[], benchmark_symbol=None)
    assert s.divergence in (None, "bullish", "bearish")
