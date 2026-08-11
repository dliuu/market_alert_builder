"""Stage ② normalize: a pure replay of raw_payloads into bars_daily.

Given the same stored payloads, ``bars_from_payloads`` always yields the same
bars — which is what lets a wiped bars_daily be rebuilt byte-for-byte from
raw_payloads (the M2 definition of done). No network, no clock, no float.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.ingest import ENDPOINT, SOURCE


class Bar(NamedTuple):
    symbol: str
    session_date: date
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741 — matches the bars_daily column name
    c: Decimal
    v: int
    adj_c: Decimal


class Payload(NamedTuple):
    symbol: str
    fetched_at: datetime
    body: str


_SELECT = text("""
    SELECT symbol, fetched_at, body::text AS body
    FROM raw_payloads
    WHERE source = :source AND endpoint = :endpoint
    ORDER BY fetched_at, id
""")

_UPSERT = text("""
    INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c)
    VALUES (:symbol, :session_date, :o, :h, :l, :c, :v, :adj_c)
    ON CONFLICT (symbol, session_date) DO UPDATE SET
        o = EXCLUDED.o, h = EXCLUDED.h, l = EXCLUDED.l, c = EXCLUDED.c,
        v = EXCLUDED.v, adj_c = EXCLUDED.adj_c
""")


def bars_from_payloads(payloads: list[Payload]) -> list[Bar]:
    """Explode payloads into bars, the latest fetch winning where sessions
    overlap. Pure and deterministic for a fixed input."""
    latest: dict[tuple[str, date], tuple[datetime, Bar]] = {}
    for payload in payloads:
        for record in json.loads(payload.body, parse_float=Decimal):
            bar = _bar(payload.symbol, record)
            key = (bar.symbol, bar.session_date)
            current = latest.get(key)
            if current is None or payload.fetched_at >= current[0]:
                latest[key] = (payload.fetched_at, bar)
    return [bar for _, (_, bar) in sorted(latest.items(), key=lambda item: item[0])]


def normalize_bars(conn: Connection, symbols: list[str] | None = None) -> int:
    """Replay stored payloads into bars_daily. Returns the number of bars
    written. Idempotent: re-running reproduces identical rows."""
    rows = conn.execute(_SELECT, {"source": SOURCE, "endpoint": ENDPOINT}).mappings().all()
    wanted = {s.upper() for s in symbols} if symbols else None
    payloads = [
        Payload(symbol=row["symbol"], fetched_at=row["fetched_at"], body=row["body"])
        for row in rows
        if wanted is None or row["symbol"] in wanted
    ]
    bars = bars_from_payloads(payloads)
    for bar in bars:
        conn.execute(_UPSERT, bar._asdict())
    return len(bars)


def _bar(symbol: str, record: dict[str, Any]) -> Bar:
    return Bar(
        symbol=symbol,
        session_date=date.fromisoformat(_as_str(record["date"])[:10]),
        o=_dec(record["open"]),
        h=_dec(record["high"]),
        l=_dec(record["low"]),
        c=_dec(record["close"]),
        v=int(_dec(record["volume"])),
        adj_c=_dec(record["adjClose"]),
    )


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _as_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")
    return value
