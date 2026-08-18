"""Stage ③ technical snapshot: moving averages, ATR, volume ratios and
support/resistance zones from adjusted daily OHLCV (M19).

Pure and exact — every aggregate is a ``Fraction`` over ``Decimal`` prices and
integer volumes, so the whole module is deterministic and unit-testable without
a vendor. Kept in its own module for the same reason ``tape.py`` is: the
M3-certified P&L ``compute`` stays untouched, and nothing here may raise on
short history the way ``compute`` does — an indicator without enough bars is
``None``, never an exception.

Everything reads the *adjusted* series. Levels drawn from raw ``h``/``l`` are
silently wrong across a split, which over a 252-session window is the difference
between a real level and a fiction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import NamedTuple

from sqlalchemy import RowMapping, text
from sqlalchemy.engine import Connection

# Simple means over the last N sessions, today included — a moving average is a
# statement about where price sits now, not about the sessions before it.
MA_WINDOWS = (20, 50, 200)

# Volume windows, in *prior* sessions. "Weekly" and "monthly" in trading days,
# not calendar days: a calendar denominator lurches around holidays. The
# measured session is never in the denominator (verify-numbers check 6), which
# is the same rule `tape.py` follows for its 30-day RVOL. That RVOL is left
# alone; three volume ratios coexist, as `premarket_vol_mult` already does.
VOL_WINDOWS = (5, 21)

# Wilder's ATR smooths exponentially; this is a simple mean of the last 14 true
# ranges. Deliberately not Wilder's — a simple mean is exact in Fraction, and an
# EMA's seeding convention is one more thing to get subtly wrong.
ATR_WINDOW = 14

# A swing pivot needs k bars either side. Williams' fractals use k=2; k=3 is
# more selective, which is what a daily briefing wants. The consequence is
# structural and intended: the most recent k bars can never form a pivot,
# because a level is not a level until it has survived k more sessions.
PIVOT_K = 3

# One trading year. The 52-week extremes are reported only on a full window —
# a field labelled "52-week high" that is really a 40-week high is a lie.
LOOKBACK_SESSIONS = 252

# Zone geometry, in multiples of ATR. A fixed percentage band would mean one
# thing for a utility and something else entirely for a biotech; ATR is the
# ruler that travels. The width cap is what stops a chain of levels each just
# inside the merge radius from collapsing into one meaningless band.
MERGE_RADIUS_ATR = Fraction(1, 2)
MAX_ZONE_WIDTH_ATR = Fraction(1)

# Tighter than the merge radius, and deliberately a separate constant: "are
# these the same level?" and "did price actually test this level?" are
# different questions. At half an ATR the touch band is wider than a typical
# daily bar, and on real data every level scored dozens of touches — ASTS came
# back 61 out of 276 sessions, which tells a reader nothing.
TOUCH_RADIUS_ATR = Fraction(1, 4)

# One touch is a coincidence. Zones below this are not reported as levels.
MIN_TOUCHES = 2

# Below this there is not enough structure to draw a level from, so the level
# fields are null rather than wrong. Short of it the averages still resolve.
MIN_SESSIONS = 60

# Volume confirmation for a breakout. Deliberately the same threshold and the
# same strict `>` as `assemble._RVOL_SPIKE`, which already decides full tier —
# a second, looser volume rule here would be indefensible.
RVOL_BREAKOUT = Fraction(3, 2)

# Extremes are levels too, and the 52-week pair carries the strongest empirical
# support of anything here (George & Hwang, 2004).
EXTREME_WINDOWS = (20, 60, LOOKBACK_SESSIONS)


class TechBar(NamedTuple):
    """One session of split-adjusted OHLCV. Callers pass these oldest → newest."""

    session_date: date
    h: Decimal
    l: Decimal  # noqa: E741 — matches the OHLC column name
    c: Decimal
    v: int


class Pivot(NamedTuple):
    """A swing high or low: the price, the date, and the volume of the bar that
    formed it. Volume is carried because it weights the zone the pivot joins."""

    session_date: date
    price: Decimal
    v: int
    kind: str  # "high" | "low"


class Zone(NamedTuple):
    """A clustered price level, with the evidence behind it. ``touches`` counts
    distinct bars that came within half an ATR — not merely the pivots that
    formed the zone, which would understate a level the price kept revisiting."""

    price: Fraction
    touches: int
    last_touch: date


@dataclass(frozen=True)
class Technicals:
    symbol: str
    ma_20: Fraction | None
    ma_50: Fraction | None
    ma_200: Fraction | None
    ma_stack: str | None
    vol_vs_5d: Fraction | None
    vol_vs_21d: Fraction | None
    atr_14: Fraction | None
    high_52w: Fraction | None
    low_52w: Fraction | None
    support: Zone | None
    resistance: Zone | None
    breakout: str | None  # "up" | "down"


def technicals_for_symbol(
    symbol: str,
    bars: Sequence[TechBar],
    *,
    rvol: Fraction | None,
) -> Technicals:
    """The full snapshot for one symbol on the last session in ``bars``."""
    window = list(bars[-LOOKBACK_SESSIONS:])
    bars = window
    closes = [b.c for b in bars]
    ma = {w: _mean_of_last(closes, w) for w in MA_WINDOWS}
    volumes = [b.v for b in bars]
    vol = {w: _volume_ratio(volumes, w) for w in VOL_WINDOWS}
    zones = _reportable_zones(bars)
    close = Fraction(bars[-1].c) if bars else None
    return Technicals(
        symbol=symbol,
        ma_20=ma[20],
        ma_50=ma[50],
        ma_200=ma[200],
        ma_stack=_ma_stack(ma[20], ma[50], ma[200]),
        vol_vs_5d=vol[5],
        vol_vs_21d=vol[21],
        atr_14=_atr(bars),
        high_52w=_high_52w(bars),
        low_52w=_low_52w(bars),
        support=_nearest_below(zones, close),
        resistance=_nearest_above(zones, close),
        breakout=_breakout(bars, zones, rvol),
    )


def swing_pivots(bars: Sequence[TechBar], *, k: int = PIVOT_K) -> list[Pivot]:
    """Swing highs and lows over ``bars``, oldest → newest.

    A bar is a pivot high when its ``h`` is the *strict* maximum of the ``2k+1``
    bars centred on it, and a pivot low when its ``l`` is the strict minimum.
    Strictness matters: on a flat top two adjacent equal highs would otherwise
    each emit a level at the same price, doubling the evidence for one event.
    """
    pivots: list[Pivot] = []
    for i in range(k, len(bars) - k):
        cur = bars[i]
        neighbours = [*bars[i - k : i], *bars[i + 1 : i + k + 1]]
        if all(b.h < cur.h for b in neighbours):
            pivots.append(Pivot(cur.session_date, cur.h, cur.v, "high"))
        if all(b.l > cur.l for b in neighbours):
            pivots.append(Pivot(cur.session_date, cur.l, cur.v, "low"))
    return pivots


def _high_52w(bars: Sequence[TechBar]) -> Fraction | None:
    if len(bars) < LOOKBACK_SESSIONS:
        return None
    return Fraction(max(b.h for b in bars[-LOOKBACK_SESSIONS:]))


def _low_52w(bars: Sequence[TechBar]) -> Fraction | None:
    if len(bars) < LOOKBACK_SESSIONS:
        return None
    return Fraction(min(b.l for b in bars[-LOOKBACK_SESSIONS:]))


def _mean_of_last(values: Sequence[Decimal], window: int) -> Fraction | None:
    """Exact mean of the last ``window`` values; ``None`` when there are fewer."""
    if len(values) < window:
        return None
    return sum((Fraction(v) for v in values[-window:]), Fraction(0)) / window


def _ma_stack(short: Fraction | None, mid: Fraction | None, long: Fraction | None) -> str | None:
    """The trend regime in one word. ``None`` until all three averages exist —
    a stack is a statement about their ordering, which two of them cannot make."""
    if short is None or mid is None or long is None:
        return None
    if short > mid > long:
        return "bullish"
    if short < mid < long:
        return "bearish"
    return "mixed"


def _volume_ratio(volumes: Sequence[int], window: int) -> Fraction | None:
    """Today's volume over the mean of the ``window`` sessions *before* it.
    ``None`` when the prior window is short or entirely untraded."""
    if len(volumes) < window + 1:
        return None
    prior = volumes[-(window + 1) : -1]
    total = sum(prior)
    if total <= 0:
        return None
    return Fraction(volumes[-1] * len(prior), total)


def _true_ranges(bars: Sequence[TechBar]) -> list[Fraction]:
    """True range per session, skipping the first bar — true range is defined
    against a prior close, which the oldest bar in the series does not have."""
    ranges = []
    for prev, cur in zip(bars, bars[1:], strict=False):
        prev_c = Fraction(prev.c)
        ranges.append(
            max(
                Fraction(cur.h) - Fraction(cur.l),
                abs(Fraction(cur.h) - prev_c),
                abs(Fraction(cur.l) - prev_c),
            )
        )
    return ranges


def _atr(bars: Sequence[TechBar]) -> Fraction | None:
    ranges = _true_ranges(bars)
    if len(ranges) < ATR_WINDOW:
        return None
    window = ranges[-ATR_WINDOW:]
    return sum(window, Fraction(0)) / len(window)


def cluster_zones(pivots: Sequence[Pivot], bars: Sequence[TechBar], atr: Fraction) -> list[Zone]:
    """Merge nearby pivots into price zones, ascending, and score each by how
    many distinct bars have touched it.

    Deterministic single-pass agglomeration over the price-sorted pivots — no
    ``k`` to choose and no random initialisation, which is the reason to prefer
    it to k-means on what is a one-dimensional problem.
    """
    if atr <= 0 or not pivots:
        return []

    radius = MERGE_RADIUS_ATR * atr
    max_width = MAX_ZONE_WIDTH_ATR * atr
    ordered = sorted(pivots, key=lambda p: p.price)

    clusters: list[list[Pivot]] = [[ordered[0]]]
    for pivot in ordered[1:]:
        current = clusters[-1]
        gap = Fraction(pivot.price) - Fraction(current[-1].price)
        width = Fraction(pivot.price) - Fraction(current[0].price)
        if gap <= radius and width <= max_width:
            current.append(pivot)
        else:
            clusters.append([pivot])

    zones = []
    for members in clusters:
        price = _volume_weighted_price(members)
        visits, last = _visits(bars, price, TOUCH_RADIUS_ATR * atr)
        if last is not None:
            zones.append(Zone(price=price, touches=visits, last_touch=last))
    return zones


def _visits(
    bars: Sequence[TechBar], price: Fraction, radius: Fraction
) -> tuple[int, date | None]:
    """Count distinct *visits* to a level, not touching bars.

    Price resting on a level for three sessions visited it once; counting bars
    turns a slow drift into what looks like repeated defence. A visit starts
    when price arrives at the level and ends when it leaves.
    """
    visits = 0
    last: date | None = None
    previously_touching = False
    for bar in sorted(bars, key=lambda b: b.session_date):
        touching = _touches(bar, price, radius)
        if touching:
            if not previously_touching:
                visits += 1
            last = bar.session_date
        previously_touching = touching
    return visits, last


def _volume_weighted_price(members: Sequence[Pivot]) -> Fraction:
    """Where the shares actually changed hands, not merely where the line sits.
    Falls back to the plain mean when the whole cluster traded nothing."""
    total_v = sum(m.v for m in members)
    if total_v <= 0:
        return sum((Fraction(m.price) for m in members), Fraction(0)) / len(members)
    weighted = sum((Fraction(m.price) * m.v for m in members), Fraction(0))
    return weighted / total_v


def _touches(bar: TechBar, price: Fraction, radius: Fraction) -> bool:
    """True when one of the bar's *extremes* comes within ``radius`` of the
    level.

    Not "the level falls inside the bar's range": a bar that straddles a level
    with both extremes far from it went straight through, which is a breach and
    evidence *against* the level, not a test of it.
    """
    return min(
        abs(Fraction(bar.h) - price), abs(Fraction(bar.l) - price)
    ) <= radius


def _reportable_zones(bars: Sequence[TechBar]) -> list[Zone]:
    """Clustered levels with enough evidence behind them to name."""
    atr = _atr(bars)
    if len(bars) < MIN_SESSIONS or atr is None:
        return []
    candidates = [*swing_pivots(bars), *_extreme_candidates(bars)]
    return [z for z in cluster_zones(candidates, bars, atr) if z.touches >= MIN_TOUCHES]


def _extreme_candidates(bars: Sequence[TechBar]) -> list[Pivot]:
    """The rolling high and low of each extreme window, attributed to the bar
    that actually set it so the zone keeps a real date and a real volume."""
    out: list[Pivot] = []
    for w in EXTREME_WINDOWS:
        if len(bars) < w:
            continue
        recent = bars[-w:]
        top = max(recent, key=lambda b: b.h)
        bottom = min(recent, key=lambda b: b.l)
        out.append(Pivot(top.session_date, top.h, top.v, "high"))
        out.append(Pivot(bottom.session_date, bottom.l, bottom.v, "low"))
    return out


def _nearest_below(zones: Sequence[Zone], close: Fraction | None) -> Zone | None:
    if close is None:
        return None
    below = [z for z in zones if z.price < close]
    return max(below, key=lambda z: z.price) if below else None


def _nearest_above(zones: Sequence[Zone], close: Fraction | None) -> Zone | None:
    if close is None:
        return None
    above = [z for z in zones if z.price > close]
    return min(above, key=lambda z: z.price) if above else None


def _breakout(bars: Sequence[TechBar], zones: Sequence[Zone], rvol: Fraction | None) -> str | None:
    """A breakout is an event, not a level: price closed through a zone it was
    on the other side of yesterday, and did it on unusual volume. Without the
    volume clause this fires on every drift across a line."""
    if len(bars) < 2 or rvol is None or rvol <= RVOL_BREAKOUT:
        return None
    prev_c = Fraction(bars[-2].c)
    cur_c = Fraction(bars[-1].c)
    for zone in zones:
        if prev_c < zone.price < cur_c:
            return "up"
        if cur_c < zone.price < prev_c:
            return "down"
    return None


def to_price_space(technicals: Technicals, factor: Fraction) -> Technicals:
    """Rescale the absolute prices from adjusted space onto today's tape.

    The engine runs entirely on the split-adjusted series, so its levels are in
    adjusted units — after a 2:1 split a level of 50 adjusted is 100 on the
    tape the reader is looking at. ``factor`` is the latest bar's ``c / adj_c``.

    Ratios (the MA distances, the volume multiples) are dimensionless and are
    deliberately left alone; scaling them too would be a silent doubled lie.
    """
    return replace(
        technicals,
        ma_20=_scale(technicals.ma_20, factor),
        ma_50=_scale(technicals.ma_50, factor),
        ma_200=_scale(technicals.ma_200, factor),
        atr_14=_scale(technicals.atr_14, factor),
        high_52w=_scale(technicals.high_52w, factor),
        low_52w=_scale(technicals.low_52w, factor),
        support=_scale_zone(technicals.support, factor),
        resistance=_scale_zone(technicals.resistance, factor),
    )


def _scale(value: Fraction | None, factor: Fraction) -> Fraction | None:
    return None if value is None else value * factor


def _scale_zone(zone: Zone | None, factor: Fraction) -> Zone | None:
    """Only the price moves — the touch count and the date are evidence about
    what happened, not measurements in any price space."""
    return None if zone is None else zone._replace(price=zone.price * factor)


# --- Database layer -------------------------------------------------------

_STORE_SCALE = Decimal("0.0000000001")

# Multi-symbol and depth-bounded in one round trip — the `flags._READ_RETURNS`
# shape, not `assemble_open._READ_CLOSES`, which costs a query per symbol.
# `c` rides along so the adjusted-space levels can be put back on today's tape.
_READ_BARS = text("""
    SELECT symbol, session_date, c, adj_c, adj_h, adj_l, adj_v FROM (
        SELECT symbol, session_date, c, adj_c, adj_h, adj_l, adj_v,
               row_number() OVER (
                   PARTITION BY symbol ORDER BY session_date DESC
               ) AS rn
        FROM bars_daily
        WHERE symbol = ANY(:symbols) AND session_date <= :session_date
    ) ranked
    WHERE rn <= :depth
    ORDER BY symbol, session_date
""")

_UPSERT = text("""
    INSERT INTO metrics (user_id, symbol, session_date, metric, value)
    VALUES (:user_id, :symbol, :session_date, :metric, :value)
    ON CONFLICT (user_id, symbol, session_date, metric)
    DO UPDATE SET value = EXCLUDED.value
""")


def compute_and_store_technicals(
    conn: Connection,
    user_id: str,
    symbols: list[str],
    session_date: date,
    *,
    rvol: Mapping[str, Fraction | None],
) -> dict[str, Technicals]:
    """Technicals for ``symbols`` on ``session_date``, persisted to ``metrics``
    and returned keyed by symbol.

    A symbol is simply absent from the result when it has no bar on the session,
    or when any bar in its window lacks the adjusted series. That second guard
    is the important one: a null ``adj_h`` means the row predates the M19 replay
    (or came from a feed that never sends it), and computing levels from the raw
    high instead would produce a confident, wrong number rather than a null.
    """
    if not symbols:
        return {}

    windows: dict[str, list[RowMapping]] = {}
    for row in conn.execute(
        _READ_BARS,
        {"symbols": symbols, "session_date": session_date, "depth": LOOKBACK_SESSIONS},
    ).mappings():
        windows.setdefault(str(row["symbol"]), []).append(row)

    out: dict[str, Technicals] = {}
    for symbol, rows in windows.items():
        bars = _tech_bars(rows)
        if bars is None or bars[-1].session_date != session_date:
            continue
        factor = _price_space_factor(rows[-1])
        if factor is None:
            continue
        out[symbol] = to_price_space(
            technicals_for_symbol(symbol, bars, rvol=rvol.get(symbol)), factor
        )

    _store(conn, user_id, session_date, out)
    return out


def _tech_bars(rows: Sequence[RowMapping]) -> list[TechBar] | None:
    """Adjusted bars for one symbol, or ``None`` when the window is incomplete."""
    bars = []
    for row in rows:
        adj_h, adj_l, adj_c, adj_v = (
            row["adj_h"],
            row["adj_l"],
            row["adj_c"],
            row["adj_v"],
        )
        if adj_h is None or adj_l is None or adj_c is None or adj_v is None:
            return None
        session_date = row["session_date"]
        assert isinstance(session_date, date)
        bars.append(
            TechBar(
                session_date=session_date,
                h=Decimal(str(adj_h)),
                l=Decimal(str(adj_l)),
                c=Decimal(str(adj_c)),
                v=int(str(adj_v)),
            )
        )
    return bars


def _price_space_factor(latest: RowMapping) -> Fraction | None:
    """``c / adj_c`` on the newest bar — 1 for a name that never split."""
    c, adj_c = latest["c"], latest["adj_c"]
    if c is None or adj_c is None or Decimal(str(adj_c)) == 0:
        return None
    return Fraction(Decimal(str(c))) / Fraction(Decimal(str(adj_c)))


def _store(
    conn: Connection, user_id: str, session_date: date, technicals: dict[str, Technicals]
) -> None:
    """Persist the numeric metrics. `ma_stack`, `breakout` and the touch dates
    are deliberately absent: `metrics.value` is `numeric`, and coercing an enum
    or a date into it to save a migration would be the wrong trade. Assembly
    reads those off the returned dataclass."""
    for t in technicals.values():
        values: dict[str, Fraction | int | None] = {
            "ma_20": t.ma_20,
            "ma_50": t.ma_50,
            "ma_200": t.ma_200,
            "vol_vs_5d": t.vol_vs_5d,
            "vol_vs_21d": t.vol_vs_21d,
            "atr_14": t.atr_14,
            "high_52w": t.high_52w,
            "low_52w": t.low_52w,
            "support": t.support.price if t.support else None,
            "resistance": t.resistance.price if t.resistance else None,
            "support_touches": t.support.touches if t.support else None,
            "resistance_touches": t.resistance.touches if t.resistance else None,
        }
        for metric, value in values.items():
            if value is None:
                continue
            conn.execute(
                _UPSERT,
                {
                    "user_id": user_id,
                    "symbol": t.symbol,
                    "session_date": session_date,
                    "metric": metric,
                    "value": _to_decimal(Fraction(value)),
                },
            )


def _to_decimal(value: Fraction) -> Decimal:
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        _STORE_SCALE, rounding=ROUND_HALF_UP
    )
