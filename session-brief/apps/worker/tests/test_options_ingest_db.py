"""M21 options ingest against the database: banded raw payloads, selected
snapshots, index history, idempotency, and a zero-network rebuild."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.calendar import previous_session
from worker.options_ingest import pull_options, rebuild_options
from worker.providers.synthetic import SyntheticOptionsProvider

# Late in exchange_calendars' window (it ends a year out) and well clear of any
# real feed in a shared dev database; the rolled-back transaction only protects
# rows this test inserts, not collisions with rows that already exist.
SESSIONS = (date(2027, 9, 14), date(2027, 9, 15))
CLOSES = {"ZZOPT": Decimal("100.00"), "ZZOPU": Decimal("250.00")}
SYMBOLS = sorted(CLOSES)


def _pull_both(conn: Connection) -> None:
    for session in SESSIONS:
        result = pull_options(conn, SyntheticOptionsProvider(CLOSES, session), SYMBOLS, session)
        assert result.symbols_failed == {}
        assert result.indices_failed == {}


def _counts(conn: Connection) -> dict[str, int]:
    queries = {
        "raw": "SELECT count(*) FROM raw_payloads WHERE source = 'cboe'",
        "contracts": "SELECT count(*) FROM option_contracts WHERE symbol LIKE 'ZZOP%'",
        "snapshots": "SELECT count(*) FROM option_snapshot WHERE contract LIKE 'ZZOP%'",
        "index": "SELECT count(*) FROM index_daily",
    }
    return {k: int(conn.execute(text(q)).scalar_one()) for k, q in queries.items()}


def _snapshots(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text("SELECT * FROM option_snapshot WHERE contract LIKE 'ZZOP%' ORDER BY 1, 2")
    ).all()
    return sorted(tuple(r) for r in rows)


def test_one_banded_chain_row_per_symbol_per_session(db_conn: Connection) -> None:
    _pull_both(db_conn)
    rows = db_conn.execute(
        text("""
            SELECT symbol, as_of, covers_from FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'delayed_quotes/options'
            ORDER BY as_of, symbol
        """)
    ).all()
    assert [tuple(r) for r in rows] == [(s, d, d) for d in SESSIONS for s in SYMBOLS]


def test_snapshots_hold_only_the_selected_contracts(db_conn: Connection) -> None:
    _pull_both(db_conn)
    rows = db_conn.execute(
        text("""
            SELECT c.symbol, s.session_date, count(*) FROM option_snapshot s
            JOIN option_contracts c USING (contract)
            WHERE c.symbol LIKE 'ZZOP%'
            GROUP BY 1, 2
        """)
    ).all()
    assert len(rows) == len(SYMBOLS) * len(SESSIONS)
    assert all(12 <= n <= 40 for _, _, n in rows)
    total_contracts = db_conn.execute(
        text("SELECT count(*) FROM option_contracts WHERE symbol LIKE 'ZZOP%'")
    ).scalar_one()
    assert total_contracts > sum(n for _, _, n in rows) / len(SESSIONS)


def test_first_seen_is_kept_from_the_first_session(db_conn: Connection) -> None:
    _pull_both(db_conn)
    seen = {
        r[0]
        for r in db_conn.execute(
            text("SELECT DISTINCT first_seen FROM option_contracts WHERE symbol LIKE 'ZZOP%'")
        )
    }
    assert SESSIONS[0] in seen
    contracts_in_both = db_conn.execute(
        text("""
            SELECT c.first_seen FROM option_contracts c
            WHERE c.symbol LIKE 'ZZOP%' AND (
                SELECT count(*) FROM option_snapshot s WHERE s.contract = c.contract
            ) = 2
        """)
    ).all()
    assert contracts_in_both
    assert all(r[0] == SESSIONS[0] for r in contracts_in_both)


def test_index_daily_has_history_and_todays_row(db_conn: Connection) -> None:
    _pull_both(db_conn)
    for symbol in ("VIX", "VIX9D", "VIX3M"):
        n = db_conn.execute(
            text("SELECT count(*) FROM index_daily WHERE symbol = :s"), {"s": symbol}
        ).scalar_one()
        assert n >= 300
    today = db_conn.execute(
        text("SELECT c FROM index_daily WHERE symbol = 'VIX' AND session_date = :d"),
        {"d": SESSIONS[1]},
    ).scalar_one()
    expected = SyntheticOptionsProvider(CLOSES, SESSIONS[1]).index_quote("VIX")["data"]["close"]
    assert today == expected
    quote_rows = db_conn.execute(
        text("""
            SELECT symbol, body FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'delayed_quotes/index'
        """)
    ).all()
    assert {r[0] for r in quote_rows} == {"_VIX", "_VIX9D", "_VIX3M"}
    assert all("options" not in r[1]["data"] for r in quote_rows)


def test_pulling_twice_is_idempotent(db_conn: Connection) -> None:
    _pull_both(db_conn)
    before = _counts(db_conn)
    snapshots = _snapshots(db_conn)
    _pull_both(db_conn)
    assert _counts(db_conn) == before
    assert _snapshots(db_conn) == snapshots


def test_rebuild_restores_identical_snapshots_with_no_network(db_conn: Connection) -> None:
    _pull_both(db_conn)
    expected = _snapshots(db_conn)
    db_conn.execute(text("DELETE FROM option_snapshot WHERE contract LIKE 'ZZOP%'"))

    def _no_network(*_: object, **__: object) -> None:
        raise AssertionError("rebuild_options touched a provider")

    with (
        patch.object(httpx.Client, "send", _no_network),
        patch.object(SyntheticOptionsProvider, "chain", _no_network),
        patch.object(SyntheticOptionsProvider, "index_quote", _no_network),
        patch.object(SyntheticOptionsProvider, "index_history", _no_network),
    ):
        written = sum(rebuild_options(db_conn, session) for session in SESSIONS)

    assert written == len(expected)
    assert _snapshots(db_conn) == expected


def test_first_capture_wins_on_a_changed_re_pull(db_conn: Connection) -> None:
    session = SESSIONS[0]
    pull_options(db_conn, SyntheticOptionsProvider(CLOSES, session), SYMBOLS, session)
    first = _snapshots(db_conn)
    moved = {s: c + Decimal("3.00") for s, c in CLOSES.items()}
    result = pull_options(db_conn, SyntheticOptionsProvider(moved, session), SYMBOLS, session)
    assert result.symbols_ok == tuple(SYMBOLS)
    assert _snapshots(db_conn) == first

    db_conn.execute(text("DELETE FROM option_snapshot WHERE contract LIKE 'ZZOP%'"))
    rebuild_options(db_conn, session)
    assert _snapshots(db_conn) == first


def test_history_replaces_the_provisional_quote_row(db_conn: Connection) -> None:
    session = SESSIONS[1]
    provisional = previous_session(session)
    db_conn.execute(
        text("INSERT INTO index_daily (symbol, session_date, c) VALUES ('VIX', :d, 99)"),
        {"d": provisional},
    )
    provider = SyntheticOptionsProvider(CLOSES, session)
    result = pull_options(db_conn, provider, [], session, indices=("VIX",))
    assert result.indices_ok == ("VIX",)

    line = next(
        ln
        for ln in provider.index_history("VIX").splitlines()
        if ln.startswith(f"{provisional:%m/%d/%Y},")
    )
    level = Decimal(line.split(",")[4])
    row = db_conn.execute(
        text("SELECT o, h, l, c FROM index_daily WHERE symbol = 'VIX' AND session_date = :d"),
        {"d": provisional},
    ).one()
    assert tuple(row) == (level, level, level, level)
    body = db_conn.execute(
        text("""
            SELECT body FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'us_indices/daily_prices'
              AND symbol = 'VIX' AND as_of = :d
        """),
        {"d": session},
    ).scalar_one()
    assert line in body["rows"]
