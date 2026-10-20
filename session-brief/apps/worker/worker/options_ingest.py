"""Options ingest (M21): pull chains and the VIX family, store, rebuild.

Per underlying, the chain is cut to the D38 band before anything is written,
then stored verbatim in ``raw_payloads`` (invariant 5) and replayed into
``option_contracts`` (every banded contract) and ``option_snapshot`` (only the
contracts the strip and IV30 consult — ``selected_contracts``). Per index, the
history CSV rows newer than ``index_daily``'s watermark and the quote's
underlying block are stored the same way and upserted into ``index_daily``.

A failure on one symbol or one index is recorded on ``PullResult`` and the run
continues — one name's bad chain must cost one name, not the brief.
``rebuild_options`` replays a session from ``raw_payloads`` with no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.constants import OPTIONS_INDICES
from worker.providers.base import OptionsProvider
from worker.providers.cboe import (
    FEED_ERRORS,
    filter_payload_for_storage,
    parse_chain,
    parse_index_csv,
)
from worker.providers.fdn import _safe_error
from worker.volatility import selected_contracts

CHAIN_ENDPOINT = "delayed_quotes/options"
INDEX_HISTORY_ENDPOINT = "us_indices/daily_prices"
INDEX_QUOTE_ENDPOINT = "delayed_quotes/index"

_INSERT_RAW = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, covers_from, as_of, body, fetched_at)
    VALUES ('cboe', :endpoint, :symbol, :as_of, :as_of, CAST(:body AS jsonb),
            COALESCE(:fetched_at, now()))
    ON CONFLICT (source, endpoint, symbol, covers_from, as_of) DO NOTHING
""")

_INSERT_CONTRACT = text("""
    INSERT INTO option_contracts
        (contract, symbol, expiration_date, put_or_call, strike, first_seen)
    VALUES (:contract, :symbol, :expiration_date, :put_or_call, :strike, :first_seen)
    ON CONFLICT (contract) DO NOTHING
""")

_UPSERT_SNAPSHOT = text("""
    INSERT INTO option_snapshot
        (contract, session_date, bid, bid_size, ask, ask_size, last, iv, delta, gamma,
         theta, vega, rho, open_interest, volume, prev_day_close, captured_at)
    VALUES (:contract, :session_date, :bid, :bid_size, :ask, :ask_size, :last, :iv,
            :delta, :gamma, :theta, :vega, :rho, :open_interest, :volume,
            :prev_day_close, :captured_at)
    ON CONFLICT (contract, session_date) DO UPDATE SET
        bid = EXCLUDED.bid, bid_size = EXCLUDED.bid_size,
        ask = EXCLUDED.ask, ask_size = EXCLUDED.ask_size,
        last = EXCLUDED.last, iv = EXCLUDED.iv, delta = EXCLUDED.delta,
        gamma = EXCLUDED.gamma, theta = EXCLUDED.theta, vega = EXCLUDED.vega,
        rho = EXCLUDED.rho, open_interest = EXCLUDED.open_interest,
        volume = EXCLUDED.volume, prev_day_close = EXCLUDED.prev_day_close,
        captured_at = EXCLUDED.captured_at
""")

_INDEX_WATERMARK = text("""
    SELECT MAX(session_date) FROM index_daily WHERE symbol = :symbol
""")

_UPSERT_INDEX = text("""
    INSERT INTO index_daily (symbol, session_date, o, h, l, c)
    VALUES (:symbol, :session_date, :o, :h, :l, :c)
    ON CONFLICT (symbol, session_date) DO UPDATE SET
        o = COALESCE(EXCLUDED.o, index_daily.o),
        h = COALESCE(EXCLUDED.h, index_daily.h),
        l = COALESCE(EXCLUDED.l, index_daily.l),
        c = EXCLUDED.c
""")

_SELECT_CHAINS = text("""
    SELECT body::text FROM raw_payloads
    WHERE source = 'cboe' AND endpoint = 'delayed_quotes/options' AND as_of = :session_date
    ORDER BY symbol
""")

_DELETE_SNAPSHOTS = text("""
    DELETE FROM option_snapshot s
    USING option_contracts c
    WHERE s.contract = c.contract AND c.symbol = :symbol AND s.session_date = :session_date
""")


@dataclass(frozen=True)
class PullResult:
    symbols_ok: tuple[str, ...]
    symbols_failed: dict[str, str]
    indices_ok: tuple[str, ...]
    indices_failed: dict[str, str]
    raw_rows: int


def pull_options(
    conn: Connection,
    provider: OptionsProvider,
    symbols: list[str],
    session_date: date,
    *,
    indices: tuple[str, ...] = OPTIONS_INDICES,
    now: datetime | None = None,
) -> PullResult:
    """Pull, band, store and replay every chain in ``symbols`` and every index
    in ``indices``. Never raises on a feed failure; ``now`` stamps
    ``raw_payloads.fetched_at`` (the database clock when omitted)."""
    symbols_ok: list[str] = []
    symbols_failed: dict[str, str] = {}
    indices_ok: list[str] = []
    indices_failed: dict[str, str] = {}
    raw_rows = 0

    for symbol in symbols:
        try:
            filtered = filter_payload_for_storage(provider.chain(symbol), session_date)
            captured_at = parse_chain(filtered)[0].timestamp
            raw_rows += _store_raw(conn, CHAIN_ENDPOINT, symbol, session_date, filtered, now)
            store_chain(conn, filtered, session_date, captured_at)
        except FEED_ERRORS as exc:
            symbols_failed[symbol] = _safe_error(exc)
            continue
        symbols_ok.append(symbol)

    for index in indices:
        try:
            raw_rows += _pull_index(conn, provider, index, session_date, now)
        except FEED_ERRORS as exc:
            indices_failed[index] = _safe_error(exc)
            continue
        indices_ok.append(index)

    return PullResult(
        symbols_ok=tuple(symbols_ok),
        symbols_failed=symbols_failed,
        indices_ok=tuple(indices_ok),
        indices_failed=indices_failed,
        raw_rows=raw_rows,
    )


def store_chain(
    conn: Connection, payload: dict[str, Any], session_date: date, captured_at: datetime
) -> int:
    """Upsert every option in ``payload`` into ``option_contracts`` (``first_seen``
    set on insert only) and the selected contracts into ``option_snapshot``,
    measured against the underlying ``close``. Returns snapshot rows written."""
    underlying, contracts = parse_chain(payload)
    if contracts:
        conn.execute(
            _INSERT_CONTRACT,
            [
                {
                    "contract": c.symbol,
                    "symbol": underlying.symbol,
                    "expiration_date": c.expiry,
                    "put_or_call": c.right,
                    "strike": c.strike,
                    "first_seen": session_date,
                }
                for c in contracts
            ],
        )
    selected = selected_contracts(contracts, underlying.close, session_date)
    if selected:
        conn.execute(
            _UPSERT_SNAPSHOT,
            [
                {
                    "contract": c.symbol,
                    "session_date": session_date,
                    "bid": c.bid,
                    "bid_size": c.bid_size,
                    "ask": c.ask,
                    "ask_size": c.ask_size,
                    "last": c.last,
                    "iv": c.iv,
                    "delta": c.delta,
                    "gamma": c.gamma,
                    "theta": c.theta,
                    "vega": c.vega,
                    "rho": c.rho,
                    "open_interest": c.open_interest,
                    "volume": c.volume,
                    "prev_day_close": c.prev_day_close,
                    "captured_at": captured_at,
                }
                for c in selected
            ],
        )
    return len(selected)


def rebuild_options(conn: Connection, session_date: date) -> int:
    """Replay ``session_date``'s stored chains into ``option_contracts`` and
    ``option_snapshot`` — zero network. ``option_strip`` is not touched here.
    Returns snapshot rows written."""
    written = 0
    for (body,) in conn.execute(_SELECT_CHAINS, {"session_date": session_date}).all():
        payload = json.loads(body, parse_float=Decimal)
        underlying, _ = parse_chain(payload)
        conn.execute(_DELETE_SNAPSHOTS, {"symbol": underlying.symbol, "session_date": session_date})
        written += store_chain(conn, payload, session_date, underlying.timestamp)
    return written


def _store_raw(
    conn: Connection,
    endpoint: str,
    symbol: str,
    session_date: date,
    body: dict[str, Any],
    fetched_at: datetime | None,
) -> int:
    result = conn.execute(
        _INSERT_RAW,
        {
            "endpoint": endpoint,
            "symbol": symbol,
            "as_of": session_date,
            "body": json.dumps(body, default=str),
            "fetched_at": fetched_at,
        },
    )
    return int(result.rowcount)


def _pull_index(
    conn: Connection,
    provider: OptionsProvider,
    index: str,
    session_date: date,
    fetched_at: datetime | None,
) -> int:
    """History rows past the watermark, then today's level from the quote."""
    raw_rows = 0
    csv_text = provider.index_history(index)
    bars = parse_index_csv(csv_text)
    lines = [line for line in csv_text.lstrip("﻿").splitlines()[1:] if line.strip()]
    mark = conn.execute(_INDEX_WATERMARK, {"symbol": index}).scalar()
    new = [
        (line, bar)
        for line, bar in zip(lines, bars, strict=True)
        if mark is None or bar.session_date > mark
    ]
    if new:
        raw_rows += _store_raw(
            conn,
            INDEX_HISTORY_ENDPOINT,
            index,
            session_date,
            {"rows": [line for line, _ in new]},
            fetched_at,
        )
        conn.execute(
            _UPSERT_INDEX,
            [
                {
                    "symbol": index,
                    "session_date": b.session_date,
                    "o": b.o,
                    "h": b.h,
                    "l": b.l,
                    "c": b.c,
                }
                for _, b in new
            ],
        )

    quote = provider.index_quote(index)
    data = quote["data"]
    block = {
        "timestamp": quote["timestamp"],
        "data": {k: v for k, v in data.items() if k != "options"},
    }
    vendor_symbol = "_" + index.upper().lstrip("^_")
    raw_rows += _store_raw(
        conn, INDEX_QUOTE_ENDPOINT, vendor_symbol, session_date, block, fetched_at
    )
    close = _num(data.get("close"))
    if close is None:
        raise KeyError(f"cboe index quote for {vendor_symbol}: missing close")
    conn.execute(
        _UPSERT_INDEX,
        {
            "symbol": index,
            "session_date": session_date,
            "o": _num(data.get("open")),
            "h": _num(data.get("high")),
            "l": _num(data.get("low")),
            "c": close,
        },
    )
    return raw_rows


def _num(value: object) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
