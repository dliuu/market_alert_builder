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
