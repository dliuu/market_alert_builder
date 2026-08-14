"""M17 stage ①/②: catalyst feeds into raw_payloads, then a pure replay into the
typed catalyst tables.

The two-stage shape is invariant 5 / D13, not a new idea: the verbatim vendor
body lives exactly once, in ``raw_payloads``, and everything typed replays from
it. M16's ``FdnClient`` already does this for the pre-market feed with
``store_captured_payloads``; this reuses the same table and the same unique key
rather than inventing a second capture path (D30).

``normalize_*`` is pure — no network, no database — so a frozen payload
fixture reproduces the typed rows byte for byte, the property M2 proved for
bars.

Per-symbol failure is isolated: one vendor 500 degrades one line of the brief
and is recorded on the watermark, it does not abort the run.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.catalysts import InsiderTx, ProposedSale, store_insider_txs
from worker.providers.base import CatalystProvider

# Vendor transaction-type strings mapped onto SEC Form 4 codes. Anything not
# here becomes "?" — recorded as unclassifiable rather than guessed, which is
# open question 2 expressed in the data.
_TYPE_MAP: dict[str, str] = {
    "p": "P", "purchase": "P", "buy": "P", "open market purchase": "P",
    "s": "S", "sale": "S", "sell": "S", "open market sale": "S",
    "m": "M", "exercise": "M", "option exercise": "M",
    "f": "F", "tax": "F", "tax withholding": "F", "payment of exercise price": "F",
    "a": "A", "grant": "A", "award": "A",
    "g": "G", "gift": "G",
}

_INSERT_PAYLOAD = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, as_of, body)
    VALUES ('fdn', :endpoint, :symbol, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, as_of) DO NOTHING
""")

_INSERT_PROPOSED = text("""
    INSERT INTO catalyst_proposed_sales
        (symbol, insider_name, filing_date, shares_proposed, approx_sale_date,
         broker, natural_key)
    VALUES (:symbol, :insider_name, :filing_date, :shares_proposed,
            :approx_sale_date, :broker, :natural_key)
    ON CONFLICT (natural_key) DO NOTHING
""")

_MARK_OK = text("""
    INSERT INTO catalyst_watermarks
        (source, symbol, last_success_at, last_seen_date, consecutive_fails)
    VALUES (:source, :symbol, :now, :as_of, 0)
    ON CONFLICT (source, symbol) DO UPDATE
        SET last_success_at = EXCLUDED.last_success_at,
            last_seen_date = GREATEST(
                catalyst_watermarks.last_seen_date, EXCLUDED.last_seen_date
            ),
            last_error = NULL,
            consecutive_fails = 0
""")

_MARK_FAIL = text("""
    INSERT INTO catalyst_watermarks (source, symbol, last_error, consecutive_fails)
    VALUES (:source, :symbol, :error, 1)
    ON CONFLICT (source, symbol) DO UPDATE
        SET last_error = EXCLUDED.last_error,
            consecutive_fails = catalyst_watermarks.consecutive_fails + 1
""")


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def normalize_insider(rows: list[dict[str, Any]]) -> list[InsiderTx]:
    """Vendor shape -> typed rows. Pure. Money lands as integer cents via
    Decimal, never float: ``3 * 0.10`` is 30 cents, not 30.000000000000004."""
    out: list[InsiderTx] = []
    for r in rows:
        shares = _decimal(r.get("shares")) or Decimal(0)
        price = _decimal(r.get("price")) or Decimal(0)
        raw_type = str(r.get("transaction_type") or "").strip().lower()
        out.append(InsiderTx(
            symbol=str(r["symbol"]),
            insider_name=str(r.get("insider_name") or "unknown"),
            insider_title=(str(r["insider_title"]) if r.get("insider_title") else None),
            transaction_date=date.fromisoformat(str(r["transaction_date"])),
            filing_date=date.fromisoformat(str(r.get("filing_date") or r["transaction_date"])),
            transaction_code=_TYPE_MAP.get(raw_type, "?"),
            shares=shares,
            value_cents=int((shares * price * 100).to_integral_value()),
            shares_after=_decimal(r.get("shares_after")),
            row_id=0,  # assigned by the database on insert
        ))
    return out


def normalize_proposed(rows: list[dict[str, Any]]) -> list[ProposedSale]:
    return [
        ProposedSale(
            symbol=str(r["symbol"]),
            insider_name=str(r.get("insider_name") or "unknown"),
            filing_date=date.fromisoformat(str(r["filing_date"])),
            shares_proposed=_decimal(r.get("shares_proposed")) or Decimal(0),
            approx_sale_date=(
                date.fromisoformat(str(r["approx_sale_date"]))
                if r.get("approx_sale_date") else None
            ),
            row_id=0,
        )
        for r in rows
    ]


def ingest_catalysts(
    conn: Connection,
    provider: CatalystProvider,
    symbols: list[str],
    *,
    as_of: date,
) -> dict[str, int]:
    """Pull both event-stream feeds for ``symbols``, store the payloads
    verbatim, and replay them into the typed tables.

    Returns per-source counts of newly typed rows. A symbol whose pull raises is
    recorded on ``catalyst_watermarks`` and skipped — the run continues, because
    a failed ETF pull must degrade one line rather than kill the section.
    """
    counts = {"insider": 0, "proposed": 0}
    now = datetime.now(UTC)

    for symbol in symbols:
        counts["insider"] += _ingest_one(
            conn, symbol, "insider", "insider-transactions", as_of, now,
            lambda s: provider.insider_transactions(s),
            lambda rows: store_insider_txs(conn, normalize_insider(rows)),
        )
        counts["proposed"] += _ingest_one(
            conn, symbol, "proposed", "proposed-sales", as_of, now,
            lambda s: provider.proposed_sales(s),
            lambda rows: _store_proposed(conn, normalize_proposed(rows)),
        )
    return counts


def _ingest_one(
    conn: Connection,
    symbol: str,
    source: str,
    endpoint: str,
    as_of: date,
    now: datetime,
    fetch: Any,
    store: Any,
) -> int:
    try:
        rows = fetch(symbol)
    except Exception as exc:  # noqa: BLE001 - one symbol's failure is not the run's
        conn.execute(_MARK_FAIL, {"source": source, "symbol": symbol, "error": str(exc)})
        return 0

    if rows:
        conn.execute(_INSERT_PAYLOAD, {
            "endpoint": endpoint, "symbol": symbol, "as_of": as_of,
            "body": json.dumps(rows, default=str),
        })
    stored = int(store(rows) or 0)
    conn.execute(_MARK_OK, {"source": source, "symbol": symbol, "now": now, "as_of": as_of})
    return stored


def _store_proposed(conn: Connection, sales: list[ProposedSale]) -> int:
    import hashlib

    stored = 0
    for s in sales:
        raw = "|".join((s.symbol, s.insider_name, s.filing_date.isoformat(),
                        str(s.shares_proposed)))
        result = conn.execute(_INSERT_PROPOSED, {
            "symbol": s.symbol, "insider_name": s.insider_name,
            "filing_date": s.filing_date, "shares_proposed": s.shares_proposed,
            "approx_sale_date": s.approx_sale_date, "broker": None,
            "natural_key": hashlib.sha256(raw.encode()).hexdigest(),
        })
        stored += result.rowcount or 0
    return stored
