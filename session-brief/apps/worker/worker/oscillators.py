"""Stage ③ indicator standing: RSI, MACD, ADX, ATR and relative strength, each
ranked against that symbol's own trailing year of the same indicator (M20).

Split from ``technicals.py`` for three reasons. It answers a different question
— not "where does price stand in its structure" but "is any of that unusual for
this name". It reads a deeper window (286 sessions against 252). And its numeric
contract differs: ``technicals`` is exact ``Fraction`` because nothing there
needs ``sqrt``, but the EMAs here compound a ``Fraction`` denominator by 13 per
step, which over a 286-session window is a ~1000-bit integer carrying no
accuracy anyone can use.

So: ``Decimal`` under an explicit 28-digit context, SMA-seeded, quantized to
10 places at every recursion step. Deterministic and reproducible across runs
and machines, and still never ``float`` — which the money rule forbids and which
would make the fixture snapshots unstable.

Every series returns only its *defined* values, oldest → newest. Warmup bars
produce no entries at all, so a caller aligns by length and can never read a
seeded-but-meaningless value by index.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from worker.technicals import TechBar

PREC = 28
QUANT = Decimal("0.0000000001")

RSI_WINDOW = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_WINDOW = 14
ATR_WINDOW = 14


def _q(value: Decimal) -> Decimal:
    """Quantize to 10dp. Applied at every recursion step so a series computed
    from a longer prefix of the same data is identical over the overlap."""
    return value.quantize(QUANT)


def _sma(values: Sequence[Decimal], window: int) -> Decimal:
    return _q(sum(values[:window], Decimal(0)) / Decimal(window))


def _ema(values: Sequence[Decimal], window: int) -> list[Decimal]:
    """SMA-seeded EMA. Returns ``len(values) - window + 1`` entries; empty when
    there is not enough data to seed."""
    if len(values) < window:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        k = Decimal(2) / Decimal(window + 1)
        current = _sma(values, window)
        out = [current]
        for v in values[window:]:
            current = _q((v - current) * k + current)
            out.append(current)
        return out


def _wilder(values: Sequence[Decimal], window: int) -> list[Decimal]:
    """Wilder's smoothing of a *mean*: seed with the SMA, then
    ``(prev * (n - 1) + x) / n``. Used by RSI and by ADX's DX average."""
    if len(values) < window:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        current = _sma(values, window)
        out = [current]
        for v in values[window:]:
            current = _q((current * Decimal(window - 1) + v) / Decimal(window))
            out.append(current)
        return out


def _wilder_sum(values: Sequence[Decimal], window: int) -> list[Decimal]:
    """Wilder's smoothing of a *sum*: seed with the sum of the first ``window``,
    then ``prev - prev / n + x``. This is the form +DM/-DM/TR use inside ADX,
    and it is NOT the same recursion as ``_wilder`` above."""
    if len(values) < window:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        current = _q(sum(values[:window], Decimal(0)))
        out = [current]
        for v in values[window:]:
            current = _q(current - current / Decimal(window) + v)
            out.append(current)
        return out


def rsi_series(closes: Sequence[Decimal]) -> list[Decimal]:
    """Wilder's RSI(14). ``len(closes) - 14`` entries.

    An all-gains window has zero average loss and RSI is 100 by definition —
    returned explicitly rather than reached by dividing by zero.
    """
    if len(closes) <= RSI_WINDOW:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        gains, losses = [], []
        for prev, cur in zip(closes, closes[1:], strict=False):
            delta = cur - prev
            gains.append(delta if delta > 0 else Decimal(0))
            losses.append(-delta if delta < 0 else Decimal(0))
        avg_gain = _wilder(gains, RSI_WINDOW)
        avg_loss = _wilder(losses, RSI_WINDOW)
        out = []
        for g, loss in zip(avg_gain, avg_loss, strict=True):
            if loss == 0:
                out.append(Decimal(100))
            elif g == 0:
                out.append(Decimal(0))
            else:
                rs = g / loss
                out.append(_q(Decimal(100) - Decimal(100) / (Decimal(1) + rs)))
        return out


def _macd_line(closes: Sequence[Decimal]) -> list[Decimal]:
    """EMA(12) - EMA(26), aligned on the slower EMA's start."""
    fast = _ema(closes, MACD_FAST)
    slow = _ema(closes, MACD_SLOW)
    if not slow:
        return []
    aligned_fast = fast[len(fast) - len(slow):]
    return [_q(f - s) for f, s in zip(aligned_fast, slow, strict=True)]


def macd_hist_series(closes: Sequence[Decimal]) -> list[Decimal]:
    """MACD histogram: line less its EMA(9) signal. The line and the signal are
    deliberately not exposed to the brief — they are two more numbers saying
    what the histogram already says."""
    line = _macd_line(closes)
    signal = _ema(line, MACD_SIGNAL)
    if not signal:
        return []
    aligned = line[len(line) - len(signal):]
    return [_q(v - s) for v, s in zip(aligned, signal, strict=True)]


def _true_range(prev: TechBar, cur: TechBar) -> Decimal:
    return max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c))


def _directional_movement(prev: TechBar, cur: TechBar) -> tuple[Decimal, Decimal]:
    """Wilder's +DM / -DM for one bar.

    Only the larger move counts, and an inside bar (lower high AND higher low)
    contributes nothing to either — both raw moves are negative. Crediting one
    of them is the classic ADX bug and it skews +DI/-DI for the rest of the run.
    """
    up = cur.h - prev.h
    down = prev.l - cur.l
    plus = up if up > down and up > 0 else Decimal(0)
    minus = down if down > up and down > 0 else Decimal(0)
    return plus, minus


def atr_series(bars: Sequence[TechBar]) -> list[Decimal]:
    """Simple mean of the last 14 true ranges, matching ``technicals._atr``
    exactly — deliberately not Wilder's, which would need a seeding convention.

    ``technicals`` computes only today's value; a percentile needs the series.
    The equality of the last value against ``technicals._atr`` is asserted by a
    test, which is what keeps the two implementations from drifting.
    """
    if len(bars) <= ATR_WINDOW:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        trs = [_true_range(p, c) for p, c in zip(bars, bars[1:], strict=False)]
        return [
            _q(sum(trs[i - ATR_WINDOW : i], Decimal(0)) / Decimal(ATR_WINDOW))
            for i in range(ATR_WINDOW, len(trs) + 1)
        ]


def adx_series(bars: Sequence[TechBar]) -> list[Decimal]:
    """Wilder's ADX(14). ``len(bars) - 27`` entries.

    A flat tape gives +DI == -DI == 0; DX is 0 there by definition rather than
    a zero division.
    """
    if len(bars) <= 2 * ADX_WINDOW - 1:
        return []
    with localcontext() as ctx:
        ctx.prec = PREC
        trs, plus_dm, minus_dm = [], [], []
        for prev, cur in zip(bars, bars[1:], strict=False):
            trs.append(_true_range(prev, cur))
            p, m = _directional_movement(prev, cur)
            plus_dm.append(p)
            minus_dm.append(m)

        tr_s = _wilder_sum(trs, ADX_WINDOW)
        plus_s = _wilder_sum(plus_dm, ADX_WINDOW)
        minus_s = _wilder_sum(minus_dm, ADX_WINDOW)

        dx = []
        for tr, p, m in zip(tr_s, plus_s, minus_s, strict=True):
            if tr == 0:
                dx.append(Decimal(0))
                continue
            plus_di = Decimal(100) * p / tr
            minus_di = Decimal(100) * m / tr
            total = plus_di + minus_di
            dx.append(
                Decimal(0) if total == 0 else _q(Decimal(100) * abs(plus_di - minus_di) / total)
            )
        return _wilder(dx, ADX_WINDOW)


# One trading year, matching ``technicals.LOOKBACK_SESSIONS``. The baseline is
# the 252 sessions BEFORE today: the measured session is never in its own
# denominator, which is the rule `technicals` already applies to its volume
# ratios (verify-numbers check 6). One rule in the brief, not two that drift.
BASELINE_SESSIONS = 252

# 252 baseline + today + MACD's 33-bar warmup. MACD is the binding constraint:
# EMA26 seeded by SMA26 gives a first line value at bar 26, and its EMA9 signal
# seeded by SMA9 gives a first histogram at bar 34.
READ_DEPTH = BASELINE_SESSIONS + 1 + (MACD_SLOW + MACD_SIGNAL - 2)

# Relative strength is measured over 21 sessions — a trading month, the same
# unit `technicals.VOL_WINDOWS` uses for its monthly volume ratio.
REL_WINDOW = 21


@dataclass(frozen=True)
class Standing:
    """One symbol's indicator standing. Every indicator is a *pair*: the value
    and its rank against that symbol's own trailing year of the same indicator.
    A value whose baseline is short keeps the value and nulls the percentile —
    a bare value is the soup `docs/01` was right to forbid, and a percentile
    over 40 observations is worse than no percentile."""

    symbol: str
    rsi14: Decimal | None
    rsi14_pctile: Decimal | None
    macd_hist: Decimal | None
    macd_hist_pctile: Decimal | None
    adx14: Decimal | None
    adx14_pctile: Decimal | None
    atr_pct: Decimal | None
    atr_pct_pctile: Decimal | None
    rel_strength: Decimal | None
    rel_strength_pctile: Decimal | None
    rel_strength_benchmark: str | None
    divergence: str | None  # "bullish" | "bearish"


def percentile(series: Sequence[Decimal]) -> Decimal | None:
    """Rank ``series[-1]`` against the ``BASELINE_SESSIONS`` values before it.

    ``None`` below a full baseline: a field presented as a 1-year percentile
    that is really a 40-observation percentile is the same lie
    ``technicals._high_52w`` refuses to tell.

    Strict ``<`` in the counter, so a value tied with its whole history scores
    0 rather than a midpoint 50. Ties are common on ADX and a midpoint
    convention would invent movement that did not happen.
    """
    if len(series) < BASELINE_SESSIONS + 1:
        return None
    today = series[-1]
    baseline = series[-(BASELINE_SESSIONS + 1) : -1]
    with localcontext() as ctx:
        ctx.prec = PREC
        below = sum(1 for v in baseline if v < today)
        return _q(Decimal(100) * Decimal(below) / Decimal(len(baseline)))


def _last(series: Sequence[Decimal]) -> Decimal | None:
    return series[-1] if series else None


def rel_strength_series(
    closes: Sequence[Decimal],
    benchmark_closes: Sequence[Decimal],
    window: int = REL_WINDOW,
) -> list[Decimal]:
    """The symbol's ``window``-session return less the benchmark's over the same
    window, per session, oldest → newest.

    Empty when either series is short. A benchmark whose window does not cover
    the symbol's yields no relative strength at all rather than a shorter one —
    a number measured over a different span is not the same number.
    """
    n = min(len(closes), len(benchmark_closes))
    if n <= window:
        return []
    sym = list(closes[-n:])
    ben = list(benchmark_closes[-n:])
    with localcontext() as ctx:
        ctx.prec = PREC
        out = []
        for i in range(window, n):
            if sym[i - window] == 0 or ben[i - window] == 0:
                continue
            r_sym = sym[i] / sym[i - window] - Decimal(1)
            r_ben = ben[i] / ben[i - window] - Decimal(1)
            out.append(_q(r_sym - r_ben))
        return out


def standing_for_symbol(
    symbol: str,
    bars: Sequence[TechBar],
    *,
    benchmark_bars: Sequence[TechBar],
    benchmark_symbol: str | None,
) -> Standing:
    """The full standing for one symbol on the last session in ``bars``.

    Pure: no clock, no connection. ``bars`` and ``benchmark_bars`` are adjusted
    OHLCV, oldest → newest. The ATR ratio is dimensionless, so computing it in
    adjusted space gives the same answer as on today's tape and needs no
    rescaling — unlike the absolute levels `technicals.to_price_space` moves.
    """
    closes = [b.c for b in bars]
    rsi = rsi_series(closes)
    macd = macd_hist_series(closes)
    adx = adx_series(bars)

    atr = atr_series(bars)
    atr_pct: list[Decimal] = []
    if atr:
        aligned = closes[len(closes) - len(atr):]
        with localcontext() as ctx:
            ctx.prec = PREC
            atr_pct = [_q(a / c) for a, c in zip(atr, aligned, strict=True) if c != 0]

    rel: list[Decimal] = []
    if benchmark_symbol and benchmark_bars:
        rel = rel_strength_series(closes, [b.c for b in benchmark_bars])

    return Standing(
        symbol=symbol,
        rsi14=_last(rsi),
        rsi14_pctile=percentile(rsi),
        macd_hist=_last(macd),
        macd_hist_pctile=percentile(macd),
        adx14=_last(adx),
        adx14_pctile=percentile(adx),
        atr_pct=_last(atr_pct),
        atr_pct_pctile=percentile(atr_pct),
        rel_strength=_last(rel),
        rel_strength_pctile=percentile(rel),
        rel_strength_benchmark=benchmark_symbol if rel else None,
        divergence=None,  # Task 3 fills this in.
    )
