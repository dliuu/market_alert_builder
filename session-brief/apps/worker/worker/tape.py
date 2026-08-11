"""Stage ③ tape quality: RVOL and range position from daily OHLCV (D3).

Pure and exact — range position is a `Fraction` over O/H/L/C, RVOL a `Fraction`
over integer volumes — so both are deterministic and unit-testable without a
vendor. Kept in its own module so the M3-certified P&L ``compute`` stays
untouched. Gap-fill and VWAP need minute bars and are deferred (D3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

# RVOL compares today's volume to the mean of this many *prior* sessions. The
# session being measured is never in the denominator (verify-numbers check 6).
RVOL_WINDOW = 30


@dataclass(frozen=True)
class TapeMetrics:
    symbol: str
    rvol: Fraction | None
    range_position: Fraction | None


def tape_for_symbol(
    symbol: str,
    *,
    h: Decimal,
    l: Decimal,  # noqa: E741 — matches the OHLC column name
    c: Decimal,
    v: int,
    prior_volumes: Sequence[int],
) -> TapeMetrics:
    """Tape metrics for one symbol on one session. ``prior_volumes`` is the
    volume series strictly before the session, oldest → newest."""
    return TapeMetrics(
        symbol=symbol,
        rvol=_rvol(v, prior_volumes),
        range_position=_range_position(h, l, c),
    )


def _range_position(h: Decimal, l: Decimal, c: Decimal) -> Fraction | None:  # noqa: E741
    span = Fraction(h) - Fraction(l)
    if span == 0:  # h == l: a flat/halted bar — undefined, not zero.
        return None
    return (Fraction(c) - Fraction(l)) / span


def _rvol(today_v: int, prior_v: Sequence[int]) -> Fraction | None:
    if len(prior_v) < RVOL_WINDOW:
        return None
    window = prior_v[-RVOL_WINDOW:]  # the most recent RVOL_WINDOW prior sessions
    total = sum(window)
    if total <= 0:
        return None
    # today_v / (total/len) — exact, denominator excludes today.
    return Fraction(today_v * len(window), total)


# --- Database layer -------------------------------------------------------

_STORE_SCALE = Decimal("0.0000000001")

_READ_SESSION_BARS = text("""
    SELECT symbol, h, l, c, v FROM bars_daily
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
""")

_READ_PRIOR_VOL = text("""
    SELECT symbol, v FROM bars_daily
    WHERE session_date < :session_date AND symbol = ANY(:symbols)
    ORDER BY symbol, session_date
""")

_UPSERT = text("""
    INSERT INTO metrics (user_id, symbol, session_date, metric, value)
    VALUES (:user_id, :symbol, :session_date, :metric, :value)
    ON CONFLICT (user_id, symbol, session_date, metric) DO UPDATE SET value = EXCLUDED.value
""")


def compute_and_store_tape(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> dict[str, TapeMetrics]:
    """Compute tape metrics for ``symbols`` on ``session_date``, persist the
    non-null ones to ``metrics``, and return them keyed by symbol. Symbols
    without a bar on the session are simply absent from the result."""
    if not symbols:
        return {}

    prior: dict[str, list[int]] = {}
    for row in conn.execute(
        _READ_PRIOR_VOL, {"session_date": session_date, "symbols": symbols}
    ).mappings():
        prior.setdefault(row["symbol"], []).append(int(row["v"]))

    out: dict[str, TapeMetrics] = {}
    for row in conn.execute(
        _READ_SESSION_BARS, {"session_date": session_date, "symbols": symbols}
    ).mappings():
        out[row["symbol"]] = tape_for_symbol(
            row["symbol"],
            h=Decimal(str(row["h"])),
            l=Decimal(str(row["l"])),
            c=Decimal(str(row["c"])),
            v=int(row["v"]),
            prior_volumes=prior.get(row["symbol"], []),
        )

    _store(conn, user_id, session_date, out)
    return out


def _store(
    conn: Connection, user_id: str, session_date: date, tape: dict[str, TapeMetrics]
) -> None:
    for metrics in tape.values():
        values = {"rvol": metrics.rvol, "range_position": metrics.range_position}
        for metric, value in values.items():
            if value is None:
                continue
            conn.execute(
                _UPSERT,
                {
                    "user_id": user_id,
                    "symbol": metrics.symbol,
                    "session_date": session_date,
                    "metric": metric,
                    "value": _to_decimal(value),
                },
            )


def _to_decimal(value: Fraction) -> Decimal:
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        _STORE_SCALE, rounding=ROUND_HALF_UP
    )
