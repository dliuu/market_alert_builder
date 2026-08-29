# M20 — §5 Indicator Standing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add §5 "Where they stand" to the close brief — per owned stock, a small set of technical indicators each shipped with its percentile against that same symbol's own trailing 252 sessions.

**Architecture:** A new pure module `worker/oscillators.py` computes RSI(14), MACD histogram(12/26/9), ADX(14), an ATR series and a 21-session relative-strength series from adjusted daily OHLCV, then ranks today's value of each against the 252 prior values of the same indicator. A DB layer mirrors `technicals.compute_and_store_technicals`. Assembly gains a gated, capped `standing` section; the email renders up to three blocks and the web archive renders all names with sparklines.

**Tech Stack:** Python 3.12 (`Decimal`, `Fraction`, SQLAlchemy Core, pytest, ruff, mypy --strict), Next.js 15 / React Email (TypeScript, biome), Postgres 16.

**Spec:** `docs/superpowers/specs/2026-08-29-m20-indicator-standing-design.md` — read it before Task 1. The spec is the binding authority; this plan argues from it.

## Global Constraints

- **Never `float` in `worker/oscillators.py`.** All arithmetic is `Decimal` inside `decimal.localcontext(prec=28)`, quantized to `QUANT = Decimal("0.0000000001")` (10dp) at every recursion step. `float` appears only at the BriefObject edge, via the existing `_ratio`/`_distance` helpers in `assemble.py`.
- `BASELINE_SESSIONS = 252`. `READ_DEPTH = 286` (252 baseline + 1 today + 33 MACD warmup).
- **The measured session is never in its own baseline.** A percentile ranks today against the 252 values *before* it.
- **Percentile counting uses strict `<`.** A flat history scores 0, never 50.
- **A baseline shorter than 252 prior values yields `None`**, and the raw value still renders.
- **Gate is strict at the boundary:** a percentile qualifies at `> 90` or `< 10`. Exactly 90 does not qualify. This matches `assemble._RVOL_SPIKE`'s strict `>`.
- **The section carries a row per owned name with a `Standing`.** The cap is a *tier*, not a deletion: the 3 most stretched qualifying names get `tier: "full"`, everything else `tier: "brief"`. The email renders full-tier rows only; the archive renders all. Ties broken by ascending symbol.
- `schema_version` bumps **8 → 9**. Section id is **`standing`**.
- **`metrics.value` is `numeric`.** Only numbers go in `metrics`; `divergence` and `rel_strength_benchmark` ride the returned dataclass, exactly as `ma_stack` and `breakout` do.
- **§4 `tape_quality` output must be byte-identical** before and after this plan. This work adds a section; it never edits one.
- **Scope:** close brief, US, owned names only. No CN (`worker_cn.assemble` passes no standing), no open brief, no watchlist.
- Every Python file passes `uv run ruff check`, `uv run mypy --strict`, `uv run pytest`. Every TS change passes `pnpm --filter web typecheck` and `pnpm check`.
- Commit after every task. Conventional-commit prefixes (`feat(m20):`, `test(m20):`, `docs(m20):`).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/worker/worker/oscillators.py` | **Create.** Pure indicator math, the percentile mechanic, divergence, and the DB layer. |
| `apps/worker/tests/test_oscillators.py` | **Create.** Pure-function tests (Tasks 1–3). |
| `apps/worker/tests/test_oscillators_db.py` | **Create.** DB-layer tests (Task 4). |
| `apps/worker/worker/assemble.py` | **Modify.** Add `_standing` section + gate; wire `compute_and_store_standing` into `assemble_and_store`. |
| `apps/worker/tests/test_assemble.py` | **Modify.** Gate and section-shape tests. |
| `packages/contracts/brief-object.schema.json` | **Modify.** `standing` section id, 11 new row fields. |
| `apps/web/emails/close-brief.tsx` | **Modify.** `<Standing>` block renderer after §4. |
| `apps/web/app/briefs/[slug]/page.tsx` | **Modify.** Full grid + sparkline for all names. |
| `docs/*` | **Modify.** Six doc files (Task 7). |

---

## Task 1: Indicator math — RSI, MACD histogram, ADX, ATR series

**Files:**
- Create: `apps/worker/worker/oscillators.py`
- Test: `apps/worker/tests/test_oscillators.py`

**Interfaces:**
- Consumes: `worker.technicals.TechBar` (a `NamedTuple` of `session_date: date, h: Decimal, l: Decimal, c: Decimal, v: int`), and `worker.technicals._atr` for the cross-check test only.
- Produces:
  - `rsi_series(closes: Sequence[Decimal]) -> list[Decimal]`
  - `macd_hist_series(closes: Sequence[Decimal]) -> list[Decimal]`
  - `adx_series(bars: Sequence[TechBar]) -> list[Decimal]`
  - `atr_series(bars: Sequence[TechBar]) -> list[Decimal]`
  - Constants `RSI_WINDOW = 14`, `MACD_FAST = 12`, `MACD_SLOW = 26`, `MACD_SIGNAL = 9`, `ADX_WINDOW = 14`, `ATR_WINDOW = 14`, `QUANT`, `PREC = 28`.
  - **Every series returns only its defined values, oldest → newest.** Warmup bars produce no entries; the caller aligns by length, never by index.

- [ ] **Step 1: Write the failing tests**

Create `apps/worker/tests/test_oscillators.py`:

```python
"""M20 pure indicator math. Known answers are hand-computed from the fixed
series below; they are asserted at three offsets rather than only the last bar,
because an EMA seeding bug shows up early and is masked by the tail."""

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

from datetime import date, timedelta


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
    [(0, "70.46"), (10, "44.44"), (-1, "62.91")],
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.oscillators'`

- [ ] **Step 3: Write the implementation**

Create `apps/worker/worker/oscillators.py`:

```python
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
            dx.append(Decimal(0) if total == 0 else _q(Decimal(100) * abs(plus_di - minus_di) / total))
        return _wilder(dx, ADX_WINDOW)
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v`
Expected: PASS.

If a known-answer expectation in `test_rsi_known_answers` disagrees with the implementation, **hand-verify the arithmetic before changing either.** Recompute Wilder RSI at that offset on paper from `SERIES`. Correct whichever is actually wrong — do not adjust the expected value to match the code, which would turn the known-answer test into a change detector.

- [ ] **Step 5: Lint and type-check**

Run: `cd apps/worker && uv run ruff check worker/oscillators.py && uv run mypy --strict worker/oscillators.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/oscillators.py apps/worker/tests/test_oscillators.py
git commit -m "feat(m20): RSI, MACD histogram, ADX and the ATR series"
```

---

## Task 2: The percentile mechanic and the `Standing` dataclass

**Files:**
- Modify: `apps/worker/worker/oscillators.py`
- Test: `apps/worker/tests/test_oscillators.py`

**Interfaces:**
- Consumes: Task 1's four series functions.
- Produces:
  - `BASELINE_SESSIONS = 252`, `READ_DEPTH = 286`
  - `percentile(series: Sequence[Decimal]) -> Decimal | None` — ranks `series[-1]` against the 252 values before it.
  - `@dataclass(frozen=True) class Standing` with fields: `symbol: str`, `rsi14`, `rsi14_pctile`, `macd_hist`, `macd_hist_pctile`, `adx14`, `adx14_pctile`, `atr_pct`, `atr_pct_pctile`, `rel_strength`, `rel_strength_pctile` (all `Decimal | None`), `rel_strength_benchmark: str | None`, `divergence: str | None`.
  - `rel_strength_series(closes, benchmark_closes, window=REL_WINDOW) -> list[Decimal]` with `REL_WINDOW = 21`.
  - `standing_for_symbol(symbol, bars, *, benchmark_bars, benchmark_symbol) -> Standing`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/worker/tests/test_oscillators.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v -k "percentile or standing or rel_strength or read_depth"`
Expected: FAIL — `ImportError: cannot import name 'BASELINE_SESSIONS'`

- [ ] **Step 3: Implement**

Append to `apps/worker/worker/oscillators.py`:

```python
from dataclasses import dataclass

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
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v`
Expected: PASS (all of Task 1's tests still pass too).

- [ ] **Step 5: Lint and type-check**

Run: `cd apps/worker && uv run ruff check worker/oscillators.py && uv run mypy --strict worker/oscillators.py`

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/oscillators.py apps/worker/tests/test_oscillators.py
git commit -m "feat(m20): the percentile mechanic and the Standing dataclass"
```

---

## Task 3: RSI divergence

**Files:**
- Modify: `apps/worker/worker/oscillators.py`
- Test: `apps/worker/tests/test_oscillators.py`

**Interfaces:**
- Consumes: `worker.technicals.swing_pivots` (returns `list[Pivot]` where `Pivot` is a `NamedTuple(session_date, price, v, kind)` and `kind` is `"high"` or `"low"`), `PIVOT_K = 3`.
- Produces: `divergence(bars, rsi) -> str | None`, constants `DIVERGENCE_MIN_GAP = 5`, `DIVERGENCE_MAX_GAP = 60`. `standing_for_symbol` now populates `Standing.divergence`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/worker/tests/test_oscillators.py`:

```python
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
    assert divergence(bars, rsi, _rsi_at={7: Decimal("80"), 27: Decimal("60")}) == "bearish"


def test_higher_high_with_higher_rsi_fires_nothing():
    """The load-bearing negative. A detector that always fires passes the
    positive test above; only this one distinguishes it from a real one."""
    bars = _peak_series("130", "135", 20)
    rsi = rsi_series([b.c for b in bars])
    assert divergence(bars, rsi, _rsi_at={7: Decimal("60"), 27: Decimal("80")}) is None


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
    assert divergence(bars, rsi, _rsi_at={7: Decimal("20"), 27: Decimal("35")}) == "bullish"


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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v -k divergence`
Expected: FAIL — `ImportError: cannot import name 'divergence'`

- [ ] **Step 3: Implement**

Append to `apps/worker/worker/oscillators.py` (and change `divergence=None` in `standing_for_symbol` to `divergence=divergence(bars, rsi)`):

```python
from worker.technicals import PIVOT_K, swing_pivots

# Two pivots closer than this are noise; further apart than this is archaeology.
DIVERGENCE_MIN_GAP = 5
DIVERGENCE_MAX_GAP = 60


def divergence(
    bars: Sequence[TechBar],
    rsi: Sequence[Decimal],
    *,
    _rsi_at: dict[int, Decimal] | None = None,
) -> str | None:
    """``bearish`` when the last two swing highs made a higher high on a lower
    RSI; ``bullish`` on the mirror. ``None`` otherwise.

    Pivots come from ``technicals.swing_pivots`` at the existing ``PIVOT_K``
    rather than a second pivot detector — it is already tested, and already
    strict-extrema so a flat top does not emit two pivots at one price.

    The honest consequence of k=3 is that the most recent three bars can never
    be pivots, so a divergence is confirmed at least ``PIVOT_K`` sessions after
    the fact. The renderer says so rather than presenting it as today's news.

    ``_rsi_at`` is a test seam for pinning RSI at specific bar indices; it is
    never passed in production.
    """
    if len(rsi) == 0 and _rsi_at is None:
        return None

    offset = len(bars) - len(rsi)

    def rsi_at(index: int) -> Decimal | None:
        if _rsi_at is not None and index in _rsi_at:
            return _rsi_at[index]
        i = index - offset
        return rsi[i] if 0 <= i < len(rsi) else None

    index_of = {b.session_date: i for i, b in enumerate(bars)}
    pivots = swing_pivots(bars, k=PIVOT_K)

    for kind, is_divergent in (
        ("high", lambda p1, p2, r1, r2: p2 > p1 and r2 < r1),
        ("low", lambda p1, p2, r1, r2: p2 < p1 and r2 > r1),
    ):
        same = [p for p in pivots if p.kind == kind]
        if len(same) < 2:
            continue
        first, second = same[-2], same[-1]
        i1, i2 = index_of[first.session_date], index_of[second.session_date]
        if not DIVERGENCE_MIN_GAP <= i2 - i1 <= DIVERGENCE_MAX_GAP:
            continue
        r1, r2 = rsi_at(i1), rsi_at(i2)
        if r1 is None or r2 is None:
            continue
        if is_divergent(first.price, second.price, r1, r2):
            return "bearish" if kind == "high" else "bullish"
    return None
```

- [ ] **Step 4: Run the full module suite**

Run: `cd apps/worker && uv run pytest tests/test_oscillators.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and type-check**

Run: `cd apps/worker && uv run ruff check worker/oscillators.py && uv run mypy --strict worker/oscillators.py`

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/oscillators.py apps/worker/tests/test_oscillators.py
git commit -m "feat(m20): RSI divergence off the existing swing pivots"
```

---

## Task 4: The DB layer

**Files:**
- Modify: `apps/worker/worker/oscillators.py`
- Test: Create `apps/worker/tests/test_oscillators_db.py`

**Interfaces:**
- Consumes: the `bars_daily` and `sectors`/`holdings` tables; `worker.constants.BENCHMARK_SYMBOL` (`"SPY"`).
- Produces: `compute_and_store_standing(conn, user_id, symbols, session_date) -> dict[str, Standing]`.

**Pattern to follow:** `technicals.compute_and_store_technicals` at `apps/worker/worker/technicals.py:444` — read the whole module's "Database layer" section first. Reuse its `_UPSERT` shape and its `_READ_BARS` query shape (one round trip, `row_number()` partitioned by symbol, depth-bounded), but with `READ_DEPTH` and this module's own query constant.

- [ ] **Step 1: Write the failing tests**

Create `apps/worker/tests/test_oscillators_db.py`. Read `apps/worker/tests/test_technicals_db.py` first and follow its style. The fixture is `db_conn` (`tests/conftest.py`), it wraps each test in a rolled-back transaction, and it **skips when `DATABASE_URL` is unset**. It talks to a real remote Postgres, so every seed must be a single `executemany` round trip — never a per-row insert loop.

```python
"""M20 DB layer. Mirrors test_technicals_db.py's seeding style."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from worker.oscillators import READ_DEPTH, compute_and_store_standing

USER = "00000000-0000-0000-0000-000000000001"


def _seed_bars(conn, symbol: str, n: int, *, adjusted: bool = True, start: str = "100") -> date:
    """n consecutive weekday bars ending today; returns the last session_date."""
    day = date(2026, 1, 5)
    price = Decimal(start)
    rows = []
    for i in range(n):
        price = price + Decimal(i % 7) / Decimal(4) + Decimal("0.1")
        span = (price * Decimal("0.005")).quantize(Decimal("0.01"))
        rows.append(
            {
                "s": symbol, "d": day + timedelta(days=i),
                "c": price, "h": price + span, "l": price - span, "v": 1_000_000,
                "adj_c": price if adjusted else None,
                "adj_o": price if adjusted else None,
                "adj_h": (price + span) if adjusted else None,
                "adj_l": (price - span) if adjusted else None,
                "adj_v": 1_000_000 if adjusted else None,
            }
        )
    # ONE round trip. The db_conn fixture talks to a real remote Postgres —
    # tests/test_technicals_db.py takes 49s for five tests — and a loop of 286
    # single-row inserts per symbol would put this file into the minutes.
    conn.execute(
        text("""
            INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v,
                                    adj_c, adj_o, adj_h, adj_l, adj_v)
            VALUES (:s, :d, :c, :h, :l, :c, :v, :adj_c, :adj_o, :adj_h, :adj_l, :adj_v)
        """),
        rows,
    )
    return rows[-1]["d"]


def test_full_window_yields_a_standing_with_percentiles(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    assert "ASTS" in out
    assert out["ASTS"].rsi14_pctile is not None


def test_null_adjusted_bar_anywhere_in_the_window_skips_the_symbol(db_conn):
    """The M19 skip rule, re-asserted here because this module has its own
    query and can regress independently of technicals.py."""
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH - 1)
    _seed_bars(db_conn, "ASTS", 1, adjusted=False)
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    assert "ASTS" not in out


def test_symbol_with_no_bar_on_the_session_is_absent(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last + timedelta(days=3))
    assert "ASTS" not in out


def test_sector_benchmark_is_used_and_named(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "XLC", READ_DEPTH, start="80")
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    db_conn.execute(
        text("INSERT INTO sectors (id, user_id, name, benchmark_symbol, sort_order) "
             "VALUES (:i, :u, 'Comms', 'XLC', 1)"),
        {"i": "11111111-1111-1111-1111-111111111111", "u": USER},
    )
    db_conn.execute(
        text("INSERT INTO holdings (id, user_id, sector_id, symbol, status) "
             "VALUES (:i, :u, :s, 'ASTS', 'owned')"),
        {"i": "22222222-2222-2222-2222-222222222222", "u": USER,
         "s": "11111111-1111-1111-1111-111111111111"},
    )
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    assert out["ASTS"].rel_strength_benchmark == "XLC"


def test_sector_without_a_benchmark_falls_back_to_spy_and_says_so(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    assert out["ASTS"].rel_strength_benchmark == "SPY"


def test_incomplete_benchmark_window_nulls_both_relative_fields(db_conn):
    """Specifically NOT a silent fallback to SPY: falling back would change the
    claim the number makes without saying so."""
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "XLC", 30, start="80")
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    db_conn.execute(
        text("INSERT INTO sectors (id, user_id, name, benchmark_symbol, sort_order) "
             "VALUES (:i, :u, 'Comms', 'XLC', 1)"),
        {"i": "11111111-1111-1111-1111-111111111111", "u": USER},
    )
    db_conn.execute(
        text("INSERT INTO holdings (id, user_id, sector_id, symbol, status) "
             "VALUES (:i, :u, :s, 'ASTS', 'owned')"),
        {"i": "22222222-2222-2222-2222-222222222222", "u": USER,
         "s": "11111111-1111-1111-1111-111111111111"},
    )
    out = compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    assert out["ASTS"].rel_strength is None
    assert out["ASTS"].rel_strength_benchmark is None


def test_numeric_metrics_are_stored_and_enums_are_not(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    stored = {
        r[0] for r in db_conn.execute(
            text("SELECT metric FROM metrics WHERE user_id = :u AND symbol = 'ASTS' "
                 "AND session_date = :d"),
            {"u": USER, "d": last},
        )
    }
    assert {"rsi14", "macd_hist", "adx14", "atr_pct", "rsi14_pctile"} <= stored
    assert "divergence" not in stored
    assert "rel_strength_benchmark" not in stored


def test_recomputation_is_idempotent(db_conn):
    last = _seed_bars(db_conn, "ASTS", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    compute_and_store_standing(db_conn, USER, ["ASTS"], last)
    n = db_conn.execute(
        text("SELECT count(*) FROM metrics WHERE user_id = :u AND symbol = 'ASTS' "
             "AND session_date = :d AND metric = 'rsi14'"),
        {"u": USER, "d": last},
    ).scalar_one()
    assert n == 1


def test_empty_symbol_list_is_a_no_op(db_conn):
    assert compute_and_store_standing(db_conn, USER, [], date(2026, 6, 1)) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/worker && uv run pytest tests/test_oscillators_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_and_store_standing'`

If `db_conn` is not the fixture name used by `tests/test_technicals_db.py`, use that file's fixture name instead and keep everything else.

- [ ] **Step 3: Implement**

Append to `apps/worker/worker/oscillators.py`:

```python
# --- Database layer -------------------------------------------------------

from datetime import date

from sqlalchemy import RowMapping, text
from sqlalchemy.engine import Connection

from worker.constants import BENCHMARK_SYMBOL

_STORE_SCALE = Decimal("0.0000000001")

# The `technicals._READ_BARS` shape at this module's own depth. Deliberately a
# second constant rather than a parameter on the first: the two modules have
# different window requirements and coupling them means one change silently
# moves the other's baseline.
_READ_BARS = text("""
    SELECT symbol, session_date, adj_c, adj_h, adj_l, adj_v FROM (
        SELECT symbol, session_date, adj_c, adj_h, adj_l, adj_v,
               row_number() OVER (
                   PARTITION BY symbol ORDER BY session_date DESC
               ) AS rn
        FROM bars_daily
        WHERE symbol = ANY(:symbols) AND session_date <= :session_date
    ) ranked
    WHERE rn <= :depth
    ORDER BY symbol, session_date
""")

_READ_BENCHMARKS = text("""
    SELECT h.symbol AS symbol, s.benchmark_symbol AS benchmark
    FROM holdings h
    LEFT JOIN sectors s ON s.id = h.sector_id
    WHERE h.user_id = :user_id AND h.symbol = ANY(:symbols)
""")

_UPSERT = text("""
    INSERT INTO metrics (user_id, symbol, session_date, metric, value)
    VALUES (:user_id, :symbol, :session_date, :metric, :value)
    ON CONFLICT (user_id, symbol, session_date, metric)
    DO UPDATE SET value = EXCLUDED.value
""")


def compute_and_store_standing(
    conn: Connection,
    user_id: str,
    symbols: list[str],
    session_date: date,
) -> dict[str, Standing]:
    """Indicator standing for ``symbols`` on ``session_date``, persisted to
    ``metrics`` and returned keyed by symbol.

    A symbol is absent from the result when it has no bar on the session, or
    when any bar in its 286-session window lacks the adjusted series. That
    second guard is M19's and it is the important one: a null ``adj_h`` means
    the row predates the adjusted-bar replay, and computing an indicator from
    the raw column would produce a confident wrong number rather than a null.
    """
    if not symbols:
        return {}

    benchmark_of = _benchmarks(conn, user_id, symbols)
    wanted = sorted({*symbols, *(b for b in benchmark_of.values() if b)})
    windows = _read_windows(conn, wanted, session_date)

    out: dict[str, Standing] = {}
    for symbol in symbols:
        bars = windows.get(symbol)
        if bars is None or bars[-1].session_date != session_date:
            continue
        benchmark = benchmark_of.get(symbol) or BENCHMARK_SYMBOL
        benchmark_bars = windows.get(benchmark) or []
        # A benchmark whose own window is incomplete yields no relative
        # strength. Silently falling back to SPY would change what the number
        # claims without changing how it is labelled.
        usable = benchmark_bars and len(benchmark_bars) >= REL_WINDOW + 1
        out[symbol] = standing_for_symbol(
            symbol,
            bars,
            benchmark_bars=benchmark_bars if usable else [],
            benchmark_symbol=benchmark if usable else None,
        )

    _store(conn, user_id, session_date, out)
    return out


def _benchmarks(conn: Connection, user_id: str, symbols: list[str]) -> dict[str, str | None]:
    return {
        str(r["symbol"]): (str(r["benchmark"]) if r["benchmark"] else None)
        for r in conn.execute(
            _READ_BENCHMARKS, {"user_id": user_id, "symbols": symbols}
        ).mappings()
    }


def _read_windows(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, list[TechBar]]:
    rows: dict[str, list[RowMapping]] = {}
    for row in conn.execute(
        _READ_BARS,
        {"symbols": symbols, "session_date": session_date, "depth": READ_DEPTH},
    ).mappings():
        rows.setdefault(str(row["symbol"]), []).append(row)

    out: dict[str, list[TechBar]] = {}
    for symbol, symbol_rows in rows.items():
        bars = _bars(symbol_rows)
        if bars is not None:
            out[symbol] = bars
    return out


def _bars(rows: Sequence[RowMapping]) -> list[TechBar] | None:
    """Adjusted bars for one symbol, or ``None`` when any bar in the window is
    missing part of the adjusted series (the M19 rule)."""
    out = []
    for row in rows:
        adj_c, adj_h, adj_l, adj_v = (
            row["adj_c"], row["adj_h"], row["adj_l"], row["adj_v"],
        )
        if adj_c is None or adj_h is None or adj_l is None or adj_v is None:
            return None
        session_date = row["session_date"]
        assert isinstance(session_date, date)
        out.append(
            TechBar(
                session_date=session_date,
                h=Decimal(str(adj_h)),
                l=Decimal(str(adj_l)),
                c=Decimal(str(adj_c)),
                v=int(adj_v),
            )
        )
    return out


def _store(
    conn: Connection, user_id: str, session_date: date, standing: dict[str, Standing]
) -> None:
    """Persist the numeric metrics only. ``divergence`` and
    ``rel_strength_benchmark`` are deliberately absent for M19's reason:
    ``metrics.value`` is ``numeric``, and coercing an enum or a ticker into it
    to save a migration would be the wrong trade."""
    for s in standing.values():
        values: dict[str, Decimal | None] = {
            "rsi14": s.rsi14,
            "rsi14_pctile": s.rsi14_pctile,
            "macd_hist": s.macd_hist,
            "macd_hist_pctile": s.macd_hist_pctile,
            "adx14": s.adx14,
            "adx14_pctile": s.adx14_pctile,
            "atr_pct": s.atr_pct,
            "atr_pct_pctile": s.atr_pct_pctile,
            "rel_strength": s.rel_strength,
            "rel_strength_pctile": s.rel_strength_pctile,
        }
        for metric, value in values.items():
            if value is None:
                continue
            conn.execute(
                _UPSERT,
                {
                    "user_id": user_id,
                    "symbol": s.symbol,
                    "session_date": session_date,
                    "metric": metric,
                    "value": value.quantize(_STORE_SCALE),
                },
            )
```

Move the `from datetime import date` and SQLAlchemy imports to the top of the file with the others — the inline placement above is for readability of this diff only.

- [ ] **Step 4: Run the tests**

Run: `cd apps/worker && uv run pytest tests/test_oscillators_db.py tests/test_oscillators.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and type-check**

Run: `cd apps/worker && uv run ruff check worker/ && uv run mypy --strict worker/oscillators.py`

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/oscillators.py apps/worker/tests/test_oscillators_db.py
git commit -m "feat(m20): read the 286-session window and store the numeric standing"
```

---

## Task 5: Contract v9 and the gated `standing` section

**Files:**
- Modify: `packages/contracts/brief-object.schema.json`
- Modify: `apps/worker/worker/assemble.py`
- Test: `apps/worker/tests/test_assemble.py`

**Interfaces:**
- Consumes: `oscillators.Standing`, `oscillators.compute_and_store_standing`.
- Produces: a `{"id": "standing", "tier": "full", "note": str | None, "rows": [...]}` section; `assemble(..., standing=...)` keyword.

- [ ] **Step 1: Update the contract**

In `packages/contracts/brief-object.schema.json`:

1. Add `"standing"` to the `section.id` enum (after `"catalysts"`).
2. Add these eleven properties to `row.properties`, after `"breakout"`:

```json
"rsi14": {
  "type": ["number", "null"],
  "description": "Close brief §5: Wilder's RSI(14) on the adjusted close. Never rendered without `rsi14_pctile` — a bare oscillator value is the indicator soup docs/01 forbids, and the percentile is what makes it a comparison (M20)."
},
"rsi14_pctile": {
  "type": ["number", "null"], "minimum": 0, "maximum": 100,
  "description": "Close brief §5: where `rsi14` sits in this symbol's own trailing 252 sessions of RSI, 0-100. The 252 sessions BEFORE today — the measured session is never in its own baseline. Null below a full baseline rather than computed over a partial year (M20)."
},
"macd_hist": {
  "type": ["number", "null"],
  "description": "Close brief §5: MACD(12,26,9) histogram — the line less its signal. The line and signal are deliberately not carried; they are two more numbers saying what the histogram says (M20)."
},
"macd_hist_pctile": {
  "type": ["number", "null"], "minimum": 0, "maximum": 100,
  "description": "Close brief §5: `macd_hist` against its own 252-session history. MACD's 33-bar warmup is what sets the 286-session read depth (M20)."
},
"adx14": {
  "type": ["number", "null"],
  "description": "Close brief §5: Wilder's ADX(14) — trend strength irrespective of direction. Carried with no '>25 = trending' label: the percentile is the comparison this section exists to make, and a hardcoded 25 would be an unearned opinion beside an earned one (M20)."
},
"adx14_pctile": {
  "type": ["number", "null"], "minimum": 0, "maximum": 100,
  "description": "Close brief §5: `adx14` against its own 252-session history (M20)."
},
"atr_pct": {
  "type": ["number", "null"],
  "description": "Close brief §5: `atr14` as a fraction of the close. The absolute `atr14` is a price and is not comparable between an $8 and an $800 name; this is. Dimensionless, so it is computed in adjusted space and needs no rescaling (M20)."
},
"atr_pct_pctile": {
  "type": ["number", "null"], "minimum": 0, "maximum": 100,
  "description": "Close brief §5: `atr_pct` against its own 252-session history — the volatility-regime read. A bottom-decile reading is compression; a top-decile one is expansion (M20)."
},
"rel_strength_pctile": {
  "type": ["number", "null"], "minimum": 0, "maximum": 100,
  "description": "Close brief §5: `rel_strength` against its own 252-session history (M20)."
},
"rel_strength_benchmark": {
  "type": ["string", "null"],
  "description": "Close brief §5: which benchmark `rel_strength` was measured against — the holding's sector `benchmark_symbol`, or SPY when the sector has none. Carried because '+6.1% vs XLC' and '+6.1% vs SPY' are different claims and the number alone cannot distinguish them. Null whenever `rel_strength` is null (M20)."
},
"divergence": {
  "description": "Close brief §5: `bearish` when the last two swing highs made a higher high on a lower RSI, `bullish` on the mirror. Pivots are k=3, so a divergence is confirmed at least three sessions after the fact and the renderer says so (M20).",
  "anyOf": [{ "enum": ["bullish", "bearish"] }, { "type": "null" }]
}
```

3. Update the existing `rel_strength` description to note it is populated as of M20 (it currently has none — add: `"description": "Close brief §5: the symbol's 21-session return less its benchmark's over the same window, as a fraction. Declared since M5 and unpopulated until M20."`).

Run: `pnpm contracts:gen` from the repo root, and commit the regenerated TS types and Pydantic models.

- [ ] **Step 2: Write the failing tests**

Append to `apps/worker/tests/test_assemble.py` — read the file's existing helpers first and build `Standing` objects with the module's own constructor:

```python
from worker.oscillators import Standing


def _standing(symbol: str, **kw) -> Standing:
    base = dict(
        rsi14=Decimal("50"), rsi14_pctile=Decimal("50"),
        macd_hist=Decimal("0"), macd_hist_pctile=Decimal("50"),
        adx14=Decimal("20"), adx14_pctile=Decimal("50"),
        atr_pct=Decimal("0.03"), atr_pct_pctile=Decimal("50"),
        rel_strength=Decimal("0.01"), rel_strength_pctile=Decimal("50"),
        rel_strength_benchmark="SPY", divergence=None,
    )
    base.update(kw)
    return Standing(symbol=symbol, **base)  # type: ignore[arg-type]


def _section(obj, section_id: str):
    return next(s for s in obj.sections if s.id == section_id)


def _full(obj):
    """Symbols the EMAIL will show — full-tier rows only. Brief-tier rows exist
    for every other owned name and are the archive's business."""
    return [r.symbol for r in _section(obj, "standing").rows if r.tier == "full"]


def test_standing_section_is_present_and_empty_when_nothing_is_stretched():
    """An empty section with a note, NOT an omitted section — a renderer treats
    an absent section as 'not computed' and says nothing at all."""
    obj = _assemble_with(standing={"ASTS": _standing("ASTS")})
    section = _section(obj, "standing")
    assert _full(obj) == []
    assert [r.tier for r in section.rows] == ["brief"]
    assert section.note is not None


def test_percentile_above_90_qualifies_and_exactly_90_does_not():
    """The strict boundary, matching assemble._RVOL_SPIKE's strict `>`."""
    at = _assemble_with(standing={"A": _standing("A", rsi14_pctile=Decimal("90"))})
    assert _full(at) == []
    over = _assemble_with(standing={"A": _standing("A", rsi14_pctile=Decimal("90.1"))})
    assert _full(over) == ["A"]


def test_percentile_below_10_qualifies_and_exactly_10_does_not():
    at = _assemble_with(standing={"A": _standing("A", atr_pct_pctile=Decimal("10"))})
    assert _full(at) == []
    under = _assemble_with(standing={"A": _standing("A", atr_pct_pctile=Decimal("9.9"))})
    assert _full(under) == ["A"]


def test_divergence_alone_qualifies_a_mid_range_name():
    obj = _assemble_with(standing={"A": _standing("A", divergence="bearish")})
    assert _full(obj) == ["A"]


def test_breakout_from_section_4_qualifies_a_mid_range_name():
    """Proves the gate arms are OR'd and that §5 reads §4's decision rather
    than recomputing a breakout of its own."""
    obj = _assemble_with(
        standing={"A": _standing("A")},
        technicals={"A": _technicals_with_breakout("A")},
    )
    assert _full(obj) == ["A"]


def test_more_than_three_qualifying_names_are_capped_and_the_rest_noted():
    stretched = {
        s: _standing(s, rsi14_pctile=Decimal(p))
        for s, p in (("A", "99"), ("B", "98"), ("C", "97"), ("D", "96"), ("E", "95"))
    }
    obj = _assemble_with(standing=stretched)
    section = _section(obj, "standing")
    assert _full(obj) == ["A", "B", "C"]
    # The capped names are still carried, as brief-tier rows for the archive.
    assert sorted(r.symbol for r in section.rows if r.tier == "brief") == ["D", "E"]
    assert "D" in section.note and "E" in section.note


def test_cap_ties_break_by_symbol_for_determinism():
    tied = {s: _standing(s, rsi14_pctile=Decimal("99")) for s in ("E", "D", "C", "B", "A")}
    assert _full(_assemble_with(standing=tied)) == ["A", "B", "C"]


def test_null_percentiles_never_qualify_a_name():
    obj = _assemble_with(standing={"A": _standing("A", rsi14_pctile=None, atr_pct_pctile=None)})
    assert _full(obj) == []


def test_section_4_output_is_unchanged_by_this_milestone():
    """§5 adds a section; it never edits one. This is the guard."""
    without = _section(_assemble_with(standing={}), "tape_quality")
    with_standing = _section(
        _assemble_with(standing={"A": _standing("A", rsi14_pctile=Decimal("99"))}),
        "tape_quality",
    )
    assert without.model_dump() == with_standing.model_dump()
```

Write `_assemble_with(...)` and `_technicals_with_breakout(...)` as local helpers over the file's existing `assemble()` call pattern. `_assemble_with` must accept `standing` and `technicals` keywords and pass through to `assemble`.

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/worker && uv run pytest tests/test_assemble.py -v -k standing`
Expected: FAIL — `assemble() got an unexpected keyword argument 'standing'`

- [ ] **Step 4: Implement**

In `apps/worker/worker/assemble.py`:

1. Bump `SCHEMA_VERSION` to `9`.
2. Add `from worker.oscillators import Standing, compute_and_store_standing`.
3. Add `standing: dict[str, Standing] | None = None` to `assemble`'s keyword parameters (beside `technicals`).
4. Append `_standing(result.positions, standing or {}, technicals or {})` to the `sections` list, **after** `_tape_quality(...)`.
5. Add:

```python
# The decile is the gate. A percentile at exactly 90 does not qualify — the
# same strict comparison `_RVOL_SPIKE` already uses, so the brief has one
# boundary convention rather than two that can drift apart.
_STRETCHED_HIGH = Decimal("90")
_STRETCHED_LOW = Decimal("10")

# Three blocks is what the email can carry beside a §4 that already lists every
# name. The rest are named in the note and rendered in full on the archive.
_STANDING_CAP = 3

_PCTILE_FIELDS = (
    "rsi14_pctile", "macd_hist_pctile", "adx14_pctile",
    "atr_pct_pctile", "rel_strength_pctile",
)


def _stretch(s: Standing) -> Decimal:
    """How far this name's most extreme percentile sits from the middle. The
    ranking key for the cap; 0 when nothing is measurable."""
    distances = [
        abs(value - Decimal(50))
        for name in _PCTILE_FIELDS
        if (value := getattr(s, name)) is not None
    ]
    return max(distances, default=Decimal(0))


def _qualifies(s: Standing, breakout: str | None) -> bool:
    """Three OR'd arms: a percentile past either decile, a divergence, or §4's
    already-decided breakout. §5 reads that decision — it never recomputes a
    breakout of its own, which would be a second volume threshold."""
    if breakout is not None or s.divergence is not None:
        return True
    return any(
        value > _STRETCHED_HIGH or value < _STRETCHED_LOW
        for name in _PCTILE_FIELDS
        if (value := getattr(s, name)) is not None
    )


def _standing(
    positions: list[PositionMetrics],
    standing: dict[str, Standing],
    technicals: dict[str, Technicals],
) -> dict[str, object]:
    """§5 "Where they stand" — the indicator standing for names that are
    actually stretched.

    Unlike §4, this section IS gated. §4 answers "where does each position
    stand" and every name has an answer; §5 answers "is any of that unusual for
    this name", and on most sessions for most names the answer is no. A second
    untiered per-name section would say every name twice in one email.

    The section is emitted even when empty: an absent section reads to a
    renderer as "not computed", and "nothing is stretched" is information.
    """
    present = [s for p in positions if (s := standing.get(p.symbol)) is not None]
    qualifying = sorted(
        (
            s
            for s in present
            if _qualifies(s, technicals[s.symbol].breakout if s.symbol in technicals else None)
        ),
        key=lambda s: (-_stretch(s), s.symbol),
    )
    shown, overflow = qualifying[:_STANDING_CAP], qualifying[_STANDING_CAP:]
    full = {s.symbol for s in shown}

    if not qualifying:
        note = "Nothing stretched — every name inside its own 10-90th percentile band"
    elif overflow:
        note = f"{', '.join(sorted(s.symbol for s in overflow))} also stretched — see the archive"
    else:
        note = None

    # Every name with a standing gets a row; the cap is expressed as a tier so
    # the archive can be ungated while the email is capped, without assembly
    # emitting two row sets or the renderer deciding anything (D16). The
    # full-tier rows lead, in gate order; the rest follow by symbol, because a
    # reference list is scanned by name.
    rows = [_standing_row(s, "full") for s in shown]
    rows += [
        _standing_row(s, "brief")
        for s in sorted(present, key=lambda s: s.symbol)
        if s.symbol not in full
    ]
    return {"id": "standing", "tier": "full", "note": note, "rows": rows}


def _standing_row(s: Standing, tier: str) -> dict[str, object]:
    return {
        "symbol": s.symbol,
        "tier": tier,
        "rsi14": _ratio(s.rsi14),
        "rsi14_pctile": _ratio(s.rsi14_pctile),
        "macd_hist": _ratio(s.macd_hist),
        "macd_hist_pctile": _ratio(s.macd_hist_pctile),
        "adx14": _ratio(s.adx14),
        "adx14_pctile": _ratio(s.adx14_pctile),
        "atr_pct": _ratio(s.atr_pct),
        "atr_pct_pctile": _ratio(s.atr_pct_pctile),
        "rel_strength": _ratio(s.rel_strength),
        "rel_strength_pctile": _ratio(s.rel_strength_pctile),
        "rel_strength_benchmark": s.rel_strength_benchmark,
        "divergence": s.divergence,
    }
```

**`_ratio` must be widened first.** It is currently
`def _ratio(value: Fraction | None) -> float | None` (`assemble.py:379`) and
`Standing` carries `Decimal`, so `mypy --strict` rejects every call above.
Change its signature to:

```python
def _ratio(value: Fraction | Decimal | None) -> float | None:
    return None if value is None else float(value)
```

That is the whole change — the body already works for both. Do **not** add a
second helper, and do not touch any existing call site: `float(Fraction)` is
unchanged, so §4's output stays byte-identical, which
`test_section_4_output_is_unchanged_by_this_milestone` asserts.

6. In `assemble_and_store`, after the `compute_and_store_technicals(...)` call, add:

```python
    # §5's indicator standing (M20). Its own 286-session read: MACD's warmup
    # needs more history than §4's 252-session window provides.
    standing = compute_and_store_standing(conn, user_id, symbols, session_date)
```

and pass `standing=standing` to the `assemble(...)` call.

- [ ] **Step 5: Run the tests**

Run: `cd apps/worker && uv run pytest tests/ -v`
Expected: PASS. **The frozen v2/v6/v7 fixtures must still render** — if `tests/test_contract_schema.py` or a fixture snapshot fails, the cause is the `SCHEMA_VERSION` bump; freeze a v8 fixture as the existing ones were frozen (copy `tests/fixtures/close_brief.json` to `tests/fixtures/close_brief_v8.json` before regenerating), do not loosen the assertion.

- [ ] **Step 6: Regenerate contracts and type-check**

Run from repo root: `pnpm contracts:gen && pnpm --filter web typecheck`

- [ ] **Step 7: Commit**

```bash
git add packages/contracts apps/worker/worker/assemble.py apps/worker/tests/ apps/web
git commit -m "feat(m20): schema_version 9 and the gated standing section"
```

---

## Task 6: Renderers — email blocks and the web archive

**Files:**
- Modify: `apps/web/emails/close-brief.tsx`
- Modify: `apps/web/app/briefs/[slug]/page.tsx`

**Interfaces:**
- Consumes: the `standing` section from the v9 BriefObject.
- Produces: no exports; both are page/template components.

- [ ] **Step 1: Add the email section**

In `apps/web/emails/close-brief.tsx`, beside the existing `const tape = ...` lookup, add:

```tsx
const standing = brief.sections.find((s) => s.id === "standing");
```

Immediately after the §4 `<Section>` block and before the catalysts block, add:

```tsx
{/* where they stand (M20) — gated, unlike §4. Only names actually stretched
    against their own history, capped at three; the rest are in `note` and
    rendered in full on the archive. */}
{standing && (standing.rows.some((r) => r.tier === "full") || standing.note) && (
  <Section style={sec}>
    <SectionHead title="Where they stand" note="against each name's own year" />
    {standing.rows
      .filter((r) => r.tier === "full")
      .map((r) => (
        <Standing key={r.symbol} row={r} />
      ))}
    {standing.note && <p style={note}>{standing.note}.</p>}
  </Section>
)}
```

And add these components beside `Tape`:

```tsx
// One name's standing. Each indicator is a value and a bar showing where that
// value sits in this symbol's own trailing year of the same indicator. The bar
// is `RangeBar` — already proven through Outlook's Word renderer via the
// bgcolor attribute, so the percentile costs no new email-client risk.
function Standing({ row }: { row: Row }) {
  const lines: [string, string, number | null | undefined][] = [
    ["RSI(14)", numberOrDash(row.rsi14, 1), row.rsi14_pctile],
    ["MACD hist", numberOrDash(row.macd_hist, 2), row.macd_hist_pctile],
    ["ADX(14)", numberOrDash(row.adx14, 1), row.adx14_pctile],
    ["ATR %", pctOrDash(row.atr_pct), row.atr_pct_pctile],
    [
      row.rel_strength_benchmark ? `vs ${row.rel_strength_benchmark} 21d` : "vs benchmark",
      pctOrDash(row.rel_strength),
      row.rel_strength_pctile,
    ],
  ];
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <tbody>
        <tr>
          <td style={tdL} colSpan={3}>
            <span style={sym}>{row.symbol}</span>
            <Divergence kind={row.divergence} />
          </td>
        </tr>
        {lines.map(([label, value, pctile]) => (
          <tr key={label}>
            <td style={{ ...tdL, width: 130 }}>{label}</td>
            <td style={{ ...tdR, width: 70 }}>{value}</td>
            <td style={{ ...tdR, verticalAlign: "middle" }}>
              {pctile == null ? (
                <span style={mut}>—</span>
              ) : (
                <>
                  <RangeBar position={pctile / 100} />
                  <span style={levelLine}>{ordinal(Math.round(pctile))}</span>
                </>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// k=3 pivots mean the second pivot needs three more sessions before it is a
// pivot at all, so a divergence is always confirmed late. Say so rather than
// presenting it as today's news.
function Divergence({ kind }: { kind: Row["divergence"] }) {
  if (kind == null) return null;
  const bearish = kind === "bearish";
  return (
    <span style={{ ...breakoutBadge, color: bearish ? palette.ox : palette.pine }}>
      {bearish ? "⚠ bearish divergence" : "⚠ bullish divergence"} · confirmed 3 sessions
    </span>
  );
}

function numberOrDash(value: number | null | undefined, places: number): string {
  return value == null ? "—" : value.toFixed(places);
}

function ordinal(n: number): string {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  const suffix = ["th", "st", "nd", "rd"][n % 10] ?? "th";
  return `${n}${n % 10 <= 3 ? suffix : "th"}`;
}
```

If `mut`, `levelLine`, `breakoutBadge`, `palette`, `pctOrDash`, `dataTable`, `tdL`, `tdR`, `sym`, `note` or `sec` are named differently in the file, use the file's actual names — do not add duplicates.

- [ ] **Step 2: Add the web archive section**

In `apps/web/app/briefs/[slug]/page.tsx`, beside the other section lookups add `const standing = brief.sections.find((s) => s.id === "standing");`, and after the §4 card add a card rendering **every** row in `standing`, full-tier and brief-tier alike. This is what makes the archive ungated while the email stays capped — the tier is assembly's decision and the archive simply ignores it:

```tsx
{standing && (
  <section style={S.card}>
    <h2 style={S.h2}>Where they stand</h2>
    {standing.rows.length === 0 ? (
      <p style={S.muted}>{standing.note}</p>
    ) : (
      <>
        <table style={S.table}>
          <thead>
            <tr>
              <th style={S.th}>Symbol</th>
              <th style={S.thR}>RSI(14)</th>
              <th style={S.thR}>MACD hist</th>
              <th style={S.thR}>ADX(14)</th>
              <th style={S.thR}>ATR %</th>
              <th style={S.thR}>vs benchmark 21d</th>
              <th style={S.th}>Divergence</th>
            </tr>
          </thead>
          <tbody>
            {standing.rows.map((r: Row) => (
              <tr key={r.symbol}>
                <td style={S.td}>{r.symbol}</td>
                <ValueAndPctile value={r.rsi14} pctile={r.rsi14_pctile} places={1} />
                <ValueAndPctile value={r.macd_hist} pctile={r.macd_hist_pctile} places={2} />
                <ValueAndPctile value={r.adx14} pctile={r.adx14_pctile} places={1} />
                <ValueAndPctile value={r.atr_pct} pctile={r.atr_pct_pctile} percent />
                <ValueAndPctile
                  value={r.rel_strength}
                  pctile={r.rel_strength_pctile}
                  percent
                  suffix={r.rel_strength_benchmark ? ` vs ${r.rel_strength_benchmark}` : ""}
                />
                <td style={S.td}>{r.divergence ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {standing.note && <p style={S.muted}>{standing.note}</p>}
      </>
    )}
  </section>
)}
```

with:

```tsx
// A value is only ever shown beside its percentile — the pair is the unit, and
// a bare oscillator value is what docs/01 was right to call soup.
function ValueAndPctile({
  value,
  pctile,
  places = 2,
  percent = false,
  suffix = "",
}: {
  value: number | null | undefined;
  pctile: number | null | undefined;
  places?: number;
  percent?: boolean;
  suffix?: string;
}) {
  if (value == null) return <td style={S.tdR}>—</td>;
  const shown = percent ? pct(value) : value.toFixed(places);
  return (
    <td style={S.tdR}>
      {shown}
      {suffix}
      <span style={S.muted}>
        {pctile == null ? " · —" : ` · ${Math.round(pctile)}th`}
      </span>
    </td>
  );
}
```

**Note on the sparkline:** the spec calls for a 252-session SVG sparkline with the M19 support/resistance zones as bands. The BriefObject carries `support`/`resistance` but **not** the price series, so a sparkline would require a new query from the web app into `bars_daily` — which crosses the boundary that the worker owns market data and the web app renders the BriefObject. **Do not add it in this task.** Record it as deferred; it is listed in Task 7's docs as a known gap.

- [ ] **Step 3: Type-check and lint**

Run from repo root: `pnpm --filter web typecheck && pnpm check`
Expected: clean.

- [ ] **Step 4: Render and size-check**

Run: `cd apps/worker && uv run -m worker.cli brief --kind close --dry-run` (or the repo's existing render check) and confirm the rendered HTML is **under 80KB**. Report the actual byte size in the task report.

- [ ] **Step 5: Commit**

```bash
git add apps/web
git commit -m "feat(m20): render §5 in the email and the web archive"
```

---

## Task 7: Docs and the manual test runbook

**Files:**
- Modify: `docs/01-product.md`, `docs/03-data-model.md`, `docs/04-brief-object.md`, `docs/05-content-spec.md`, `docs/07-decisions.md`, `docs/08-milestones.md`
- Create: `docs/superpowers/plans/2026-08-29-m20-manual-test.md`

- [ ] **Step 1: Amend the reversed non-goal**

In `docs/01-product.md`, replace the line:

```
- **No technical indicator soup.** RSI/MACD/Bollinger across eight names is noise.
```

with:

```
- **No bare technical indicators.** *Amended by M20 (D36).* The original rule
  read "no indicator soup — RSI/MACD/Bollinger across eight names is noise",
  and it was right about bare values. §5 carries RSI, MACD and ADX under three
  conditions that keep the rule's intent: no oscillator is ever shown without
  its own-history percentile, the set is three orthogonal measures rather than
  a panel of near-duplicates, and the email section is gated at the decile and
  capped at three names. Stochastic, Williams %R, CCI and Bollinger %B remain
  ruled out as restatements of RSI.
```

- [ ] **Step 2: Fix the backfill depth**

In `docs/03-data-model.md`, change `uv run -m worker.cli backfill --days 400` to `--days 500`, and extend the surrounding paragraph:

```
Run `uv run -m worker.cli backfill --days 500` after adding a holding — one
Tiingo request per symbol regardless of window width, so it is cheap against the
free tier's 50/hour. **500, not 400:** M20's percentiles need 286 sessions
(252 baseline + today + MACD's 33-bar warmup), and 400 calendar days is only
≈275 sessions. The same applies to a new **sector benchmark**: without its own
full window, every holding in that sector renders `rel_strength` as `—`.
```

Then add an "M20 indicator standing" subsection documenting the eleven metrics and the percentile rule, in the style of the existing "The M19 technical metrics" subsection.

- [ ] **Step 3: Contract, content spec, decisions, milestones**

- `docs/04-brief-object.md`: add the `| 9 | M20 | §5 standing row fields: rsi14/macd_hist/adx14/atr_pct/rel_strength with their `*_pctile` pairs, `rel_strength_benchmark`, `divergence`; new section id `standing` |` row.
- `docs/05-content-spec.md`: insert §5 "Where they stand" into the close-brief table, renumber Sector rotation → 6, After hours → 7, Yesterday's flag → 8, and delete the "`vs sector` is still unbuilt — `rel_strength` remains a declared, unpopulated field" clause from §4's description.
- `docs/07-decisions.md`: add **D36**, following D35's structure — what it adds, the four load-bearing choices (percentile-not-value, three orthogonal measures, gated-and-capped, quantized Decimal), what each rules out, and a *Reverses if* clause: the gate firing for most names on most sessions means the percentiles are not discriminating, and the fallback is divergence-only.
- `docs/08-milestones.md`: add the M20 entry, marked `[x]`, with its definition of done.

- [ ] **Step 4: Write the manual test runbook**

Create `docs/superpowers/plans/2026-08-29-m20-manual-test.md` with these sections, each an exact command and an exact expected observation:

1. **Backfill deep enough.** `uv run -m worker.cli backfill --days 500` for every holding *and* every sector benchmark. Verify with a SQL count: each symbol has ≥286 rows in `bars_daily` with non-null `adj_h`.
2. **Compute the standing.** `uv run -m worker.cli brief --kind close --date <session> --dry-run`. Expect §5 to render with 0-3 blocks.
3. **Audit one percentile by hand.** Pick a rendered RSI percentile. Query the 253 `rsi14` values from `metrics` across prior sessions (or recompute from `bars_daily`), count how many are strictly below today's, and confirm the rendered ordinal matches. **This is the check M19's history says matters most** — its first touch predicate looked correct and was wrong on real data.
4. **Force the gate.** Temporarily lower `_STRETCHED_HIGH` to 50, re-run the dry run, confirm more names appear and the overflow note names the rest; restore it.
5. **Force the empty state.** Raise `_STRETCHED_HIGH` to 100, re-run, confirm the section renders the "nothing stretched" note with no blocks; restore it.
6. **Short-history honesty.** Add a symbol with 90 days of history to the book, re-run, confirm its values render and its percentiles render `—`, and that it never qualifies for §5.
7. **Email clients.** Send a real brief to yourself; open in Gmail (web + iOS) and Outlook. Confirm the percentile bars render, nothing is clipped, and the total size is under 80KB (Gmail clips at ~102KB).
8. **Dark mode.** Check the oxblood/pine divergence badge survives inversion in Apple Mail, per `docs/06`.
9. **§4 unchanged.** Diff the rendered §4 table against a pre-M20 brief for the same session — it must be identical.

- [ ] **Step 5: Full suite**

Run from `apps/worker`: `uv run pytest && uv run ruff check && uv run mypy --strict worker/`
Run from repo root: `pnpm check && pnpm --filter web typecheck && pnpm contracts:gen`
Expected: all clean, and `contracts:gen` produces no diff (it was already run in Task 5).

- [ ] **Step 6: Commit**

```bash
git add docs/
git commit -m "docs(m20): D36, the milestone, the amended non-goal and the manual runbook"
```

---

## Self-Review Notes

**Spec coverage:** every spec section maps to a task — indicators → T1, percentile + `Standing` → T2, divergence → T3, read depth/skip rules/`rel_strength` benchmark resolution/storage → T4, contract + gate + cap + empty state → T5, both renderers → T6, all six doc changes + runbook → T7. Spec validation checks 1-26 map to tests in T1 (1-4), T2 (5-9), T3 (10-13), T5 (14-17, 21-23, 25), T4 (18-20), T6 (24), T7 (26).

**One deviation from the spec, deliberate:** the spec's web-archive sparkline is **deferred** in Task 6. The BriefObject carries levels but not the price series, so drawing one needs a new web→`bars_daily` query, which crosses the boundary that the worker owns market data and the web app renders the BriefObject. Shipping that boundary crossing inside a rendering task would be the wrong place to decide it. The rest of the archive work — the full value+percentile grid — ships.

**Resolved during self-review, not deferred:** an earlier draft had assembly emit only the three capped rows, which would have made the archive's "all names" impossible without a second row set. Reusing `row.tier` (M5) solves it — every name with a standing gets a row, the cap becomes `full` vs `brief`, the email filters on tier, and the archive ignores it. The spec was corrected to match before this plan was finalised.
