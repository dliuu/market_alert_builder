"""Task 5 (M16): the scheduler's live/synthetic swap point and the fdn
raw-payload capture that honours invariant 5 (verbatim, never mutated).

Two tests:

- `store_captured_payloads` writes `FdnClient.captured` verbatim and is
  conflict-idempotent — a direct unit test against `db_conn` (no `Engine`
  needed, `store_captured_payloads` takes a `Connection`).
- The live branch of `ingest_premarket_for_session` (a `client` is passed):
  a held name's `quotes.extended_last` comes from a mocked minute print, not
  a hash. This needs a real `Engine` (the function opens/commits its own
  connections), so — per `tests/test_scheduler_db.py:38-46` — this module
  defines its own local `engine` fixture rather than using the shared
  transaction-scoped `db_conn` from `conftest.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from worker.providers.fdn import FdnClient, store_captured_payloads
from worker.scheduler import ingest_premarket_for_session

_SESSION = date(2026, 8, 14)


def test_store_captured_payloads_is_verbatim_and_idempotent(db_conn: Connection) -> None:
    body = '[{"trading_symbol": "ZFDN", "close": 74.31}]'
    client = FdnClient("k", transport=httpx.MockTransport(
        lambda _r: httpx.Response(200, text=body)
    ))
    client.fetch("latest-prices", identifier="ZFDN")

    assert store_captured_payloads(db_conn, client, as_of=_SESSION) == 1
    assert store_captured_payloads(db_conn, client, as_of=_SESSION) == 0  # conflict-skip

    row = db_conn.execute(text(
        "SELECT source, endpoint, symbol, body FROM raw_payloads "
        "WHERE source = 'fdn' AND symbol = 'ZFDN' AND as_of = :d"
    ), {"d": _SESSION}).mappings().one()
    assert row["endpoint"] == "latest-prices"
    assert row["body"] == [{"trading_symbol": "ZFDN", "close": 74.31}]


# --- Live branch: quotes.extended_last comes from the vendor, not a hash ---

_LIVE_SESSION = date(2099, 3, 15)
_LIVE_PRIOR = date(2099, 3, 14)
_HELD_SYMBOL = "ZFDN2"


@pytest.fixture
def engine() -> Iterator[Engine]:
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    yield get_engine()


def _mock_transport(request: httpx.Request) -> httpx.Response:
    # Only the held symbol's `latest-prices` fetch carries a real print;
    # every other fdn endpoint (the tape's futures/index/stock-quotes calls)
    # returns an empty list, which every provider path treats as "no row" —
    # this test only asserts the held name's quote.
    if "latest-prices" in str(request.url):
        return httpx.Response(
            200,
            text='[{"time": "2099-03-15 10:00:00", "close": 41.50, "volume": 500}]',
        )
    return httpx.Response(200, text="[]")


def test_live_branch_writes_extended_last_from_mocked_minute_bars(engine: Engine) -> None:
    user_id = str(uuid4())
    client = FdnClient("k", transport=httpx.MockTransport(_mock_transport))
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": user_id, "e": f"{user_id}@example.invalid"},
            )
            sector_id = conn.execute(
                text("INSERT INTO sectors (user_id, name) VALUES (:u, 'M16 Test') "
                     "RETURNING id"),
                {"u": user_id},
            ).scalar()
            conn.execute(
                text("INSERT INTO holdings (user_id, sector_id, symbol, status) "
                     "VALUES (:u, :sec, :s, 'owned')"),
                {"u": user_id, "sec": sector_id, "s": _HELD_SYMBOL},
            )
            conn.execute(
                text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, "
                     "v, adj_c) VALUES (:s, :d, 40, 40, 40, 40, 1000, 40)"),
                {"s": _HELD_SYMBOL, "d": _LIVE_PRIOR},
            )

        ingest_premarket_for_session(
            engine,
            session_date=_LIVE_SESSION,
            prior_session=_LIVE_PRIOR,
            user_id=user_id,
            client=client,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT extended_last FROM quotes "
                     "WHERE session_date = :d AND symbol = :s"),
                {"d": _LIVE_SESSION, "s": _HELD_SYMBOL},
            ).mappings().one()
        assert row["extended_last"] == Decimal("41.50")
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM quotes WHERE session_date = :d"),
                {"d": _LIVE_SESSION},
            )
            conn.execute(
                text("DELETE FROM bars_daily WHERE session_date = :d AND symbol = :s"),
                {"d": _LIVE_PRIOR, "s": _HELD_SYMBOL},
            )
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
