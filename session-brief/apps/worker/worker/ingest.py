"""Stage ① ingest: fetch vendor payloads and store them verbatim.

No transformation happens here (invariant 5). Numeric fields arrive as Decimal
from the provider and are serialized as JSON strings so exact vendor precision
survives the round-trip — normalize re-parses them as Decimal. The whole
response for a symbol is stored as one row, keyed by the latest session date it
contains, so a re-fetch of the same window is a no-op (idempotent).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.providers.base import MarketDataProvider

SOURCE = "tiingo"
ENDPOINT = "daily/prices"

_INSERT = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, as_of, body)
    VALUES (:source, :endpoint, :symbol, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, as_of) DO NOTHING
""")


def ingest_daily_bars(
    conn: Connection,
    provider: MarketDataProvider,
    symbols: list[str],
    start: date,
    end: date,
) -> int:
    """Fetch and store daily bars for each symbol. Returns the number of new
    raw_payloads rows written (0 for an already-ingested window)."""
    written = 0
    for symbol in symbols:
        sym = symbol.upper()
        records = provider.daily_bars(sym, start, end)
        if not records:
            continue
        as_of = max(_session_date(record) for record in records)
        body = json.dumps(records, default=str)
        result = conn.execute(
            _INSERT,
            {
                "source": SOURCE,
                "endpoint": ENDPOINT,
                "symbol": sym,
                "as_of": as_of,
                "body": body,
            },
        )
        written += result.rowcount
    return written


def _session_date(record: dict[str, Any]) -> date:
    raw = record["date"]
    if not isinstance(raw, str):
        raise ValueError(f"expected a string date, got {raw!r}")
    return date.fromisoformat(raw[:10])
