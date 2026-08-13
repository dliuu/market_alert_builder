"""PM synthesize / AM reconcile plumbing (M11). Silent synthetic/official drift
would slowly corrupt every fitted beta, so this is real and tested from day one.
Injected clock; idempotent on the attribution PK.

The PM run marks today's attribution rows synthetic/provisional; the AM run
compares each stored (synthetic) day-return against the official adj_c bar and,
where they diverge beyond RECONCILE_TOL, re-scores from the official bar and
flips ``revised`` — converging to the official-only result and clearing
``provisional``. A second reconcile finds no synthetic rows and is a no-op."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.attribution import BPS, score
from worker.config import RECONCILE_TOL
from worker.providers.base import MarketDataProvider


@dataclass(frozen=True)
class ReconcileResult:
    revised: list[str]
    unchanged: list[str]


def needs_revision(
    synthetic_ret: float, official_ret: float, tol: float = RECONCILE_TOL
) -> bool:
    return abs(synthetic_ret - official_ret) > tol


def synthesize_pm_bar(
    provider: MarketDataProvider, symbol: str, prev_adj_c: Decimal
) -> float:
    """Raw minute close as a session-close proxy. The mechanical ex-div gap it
    may carry is removed by the AM reconcile against the adjusted official bar."""
    minute = provider.latest_minute(symbol)
    return float(minute["price"] / prev_adj_c) - 1.0


_SYNTHETIC_SYMBOLS = text("""
    SELECT symbol, total_bps FROM attribution
    WHERE trade_date = :d AND model_version = :mv AND synthetic = true
""")

_OFFICIAL_RET = text("""
    SELECT symbol, adj_c / prev_c - 1 AS ret FROM (
        SELECT symbol, session_date, adj_c,
               LAG(adj_c) OVER (PARTITION BY symbol ORDER BY session_date) AS prev_c
        FROM bars_daily
    ) r WHERE session_date = :d AND prev_c IS NOT NULL AND prev_c <> 0
""")


def reconcile(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int
) -> ReconcileResult:
    """AM run: reconcile today's synthetic attribution rows against the official
    bar. Names whose return diverged beyond tolerance are re-scored from the
    official bar and marked revised; the rest simply converge (synthetic cleared)."""
    synthetic = conn.execute(
        _SYNTHETIC_SYMBOLS, {"d": trade_date, "mv": model_version}
    ).mappings().all()
    if not synthetic:
        return ReconcileResult(revised=[], unchanged=[])

    official = {
        row["symbol"]: float(row["ret"])
        for row in conn.execute(_OFFICIAL_RET, {"d": trade_date}).mappings()
    }

    revised: list[str] = []
    unchanged: list[str] = []
    for row in synthetic:
        symbol = row["symbol"]
        synthetic_ret = float(row["total_bps"]) / BPS
        off = official.get(symbol)
        if off is not None and needs_revision(synthetic_ret, off):
            revised.append(symbol)
        else:
            unchanged.append(symbol)

    # Re-score every synthetic name from the official bar in one pass (clears
    # synthetic/provisional, recomputes the day's basket returns), then stamp
    # revised on the names that actually diverged.
    score(conn, trade_date, now_utc=now_utc, model_version=model_version, synthetic=False)
    if revised:
        conn.execute(text("""
            UPDATE attribution SET revised = true
            WHERE trade_date = :d AND model_version = :mv AND symbol = ANY(:syms)
        """), {"d": trade_date, "mv": model_version, "syms": revised})

    return ReconcileResult(revised=revised, unchanged=unchanged)
