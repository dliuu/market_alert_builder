"""Deterministic synthetic bars for attribution tests. Writes adj_c (the
attribution return source): SPY is the market; other names load on it plus
idiosyncratic noise. A fixed RNG keeps snapshots stable, no network."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

_INSERT_BAR = text("""
    INSERT INTO bars_daily (symbol, session_date, o, h, l, c, adj_c, v)
    VALUES (:symbol, :d, :px, :px, :px, :px, :px, 1000000)
    ON CONFLICT (symbol, session_date) DO UPDATE
        SET adj_c = EXCLUDED.adj_c, c = EXCLUDED.c
""")


def seed_bars_for(
    conn: Connection, symbols: list[str], *, sessions: int,
    end: date = date(2020, 6, 30),
) -> None:
    """Seed `sessions` consecutive daily bars per symbol ending on `end`. One
    batched executemany keeps the DB round-trips down for the integration tests."""
    rng = random.Random(1234)
    dates = [end - timedelta(days=sessions - 1 - i) for i in range(sessions)]

    market_px = 100.0
    market_series: list[float] = []
    for _ in dates:
        market_px *= 1 + rng.gauss(0, 0.01)
        market_series.append(market_px)

    params: list[dict[str, object]] = []
    for symbol in symbols:
        px = 50.0
        beta = 1.0 if symbol == "SPY" else rng.uniform(0.5, 1.5)
        prev_mkt = market_series[0]
        for d, mkt in zip(dates, market_series, strict=True):
            if symbol == "SPY":
                px = mkt
            else:
                r = beta * (mkt / prev_mkt - 1) + rng.gauss(0, 0.004)
                px *= 1 + r
            prev_mkt = mkt
            params.append({"symbol": symbol, "d": d, "px": Decimal(f"{px:.4f}")})
    conn.execute(_INSERT_BAR, params)
