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
    INSERT INTO raw_payloads (source, endpoint, symbol, covers_from, as_of, body)
    VALUES (:source, :endpoint, :symbol, :covers_from, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, covers_from, as_of) DO NOTHING
""")


def ingest_daily_bars(
    conn: Connection,
    provider: MarketDataProvider,
    symbols: list[str],
    start: date,
    end: date,
    *,
    source: str = SOURCE,
    endpoint: str = ENDPOINT,
) -> int:
    """Fetch and store daily bars for each symbol. Returns the number of new
    raw_payloads rows written (0 for an already-ingested window). ``source``/
    ``endpoint`` default to the Tiingo constants; the CN backfill path passes
    ``source="synthetic-cn"`` so its rows never collide with live Tiingo rows."""
    written = 0
    for symbol in symbols:
        sym = symbol.upper()
        records = provider.daily_bars(sym, start, end)
        if not records:
            continue
        # Both ends of the window the response actually covers. `covers_from` is
        # what makes a *wider* re-fetch ending on the same day a distinct row
        # rather than a discarded duplicate (migration 0014_raw_payload_covers_from).
        sessions = [_session_date(record) for record in records]
        covers_from, as_of = min(sessions), max(sessions)
        body = json.dumps(records, default=str)
        result = conn.execute(
            _INSERT,
            {
                "source": source,
                "endpoint": endpoint,
                "symbol": sym,
                "covers_from": covers_from,
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
