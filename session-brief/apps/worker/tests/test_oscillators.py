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
