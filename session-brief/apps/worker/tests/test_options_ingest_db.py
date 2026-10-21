"""M21 options ingest against the database: banded raw payloads, selected
snapshots, index history, idempotency, and a zero-network rebuild.

Every test pulls its own fake underlyings and its own fake index names, and
every assertion is scoped to them, so the tests hold against a shared dev
database that already carries live or seeded rows (VIX, ZZDASH, ...)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.calendar import previous_session
from worker.options_ingest import pull_options, rebuild_options
from worker.providers import synthetic
from worker.providers.synthetic import SyntheticOptionsProvider

# Late in exchange_calendars' window (it ends a year out) and well clear of any
# real feed in a shared dev database; the rolled-back transaction only protects
# rows this test inserts, not collisions with rows that already exist.
SESSIONS = (date(2027, 9, 14), date(2027, 9, 15))


@pytest.fixture(autouse=True)
def _fake_index_levels() -> Iterator[None]:
    """Let the synthetic provider price this file's fake index names."""
    names = {f"ZQX{tag}{n}": Decimal(n) for tag in "ABCDEFGH" for n in range(3)}
    with patch.dict(synthetic._VIX_OFFSETS, names):
        yield


def _closes(tag: str) -> dict[str, Decimal]:
    """Two fake underlyings unique to one test."""
    return {f"ZQ{tag}A": Decimal("100.00"), f"ZQ{tag}B": Decimal("250.00")}


def _indices(tag: str) -> tuple[str, ...]:
    return tuple(f"ZQX{tag}{n}" for n in range(3))


def _pull_both(conn: Connection, tag: str) -> None:
    closes = _closes(tag)
    for session in SESSIONS:
        result = pull_options(
            conn,
            SyntheticOptionsProvider(closes, session),
            sorted(closes),
            session,
            indices=_indices(tag),
        )
        assert result.symbols_failed == {}
        assert result.indices_failed == {}


def _counts(conn: Connection, tag: str) -> dict[str, int]:
    symbols, indices = sorted(_closes(tag)), list(_indices(tag))
    params = {
        "syms": symbols,
        "ixs": indices,
        "all": symbols + indices + ["_" + i for i in indices],
    }
    queries = {
        "raw": "SELECT count(*) FROM raw_payloads WHERE source = 'cboe' AND symbol = ANY(:all)",
        "contracts": "SELECT count(*) FROM option_contracts WHERE symbol = ANY(:syms)",
        "snapshots": "SELECT count(*) FROM option_snapshot s JOIN option_contracts c "
        "USING (contract) WHERE c.symbol = ANY(:syms)",
        "index": "SELECT count(*) FROM index_daily WHERE symbol = ANY(:ixs)",
    }
    return {k: int(conn.execute(text(q), params).scalar_one()) for k, q in queries.items()}


def _snapshots(conn: Connection, tag: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text("""
            SELECT s.* FROM option_snapshot s JOIN option_contracts c USING (contract)
            WHERE c.symbol = ANY(:syms) ORDER BY 1, 2
        """),
        {"syms": sorted(_closes(tag))},
    ).all()
    return sorted(tuple(r) for r in rows)


def test_one_banded_chain_row_per_symbol_per_session(db_conn: Connection) -> None:
    _pull_both(db_conn, "A")
    symbols = sorted(_closes("A"))
    rows = db_conn.execute(
        text("""
            SELECT symbol, as_of, covers_from FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'delayed_quotes/options'
              AND symbol = ANY(:syms)
            ORDER BY as_of, symbol
        """),
        {"syms": symbols},
    ).all()
    assert [tuple(r) for r in rows] == [(s, d, d) for d in SESSIONS for s in symbols]


def test_snapshots_hold_only_the_selected_contracts(db_conn: Connection) -> None:
    _pull_both(db_conn, "B")
    symbols = sorted(_closes("B"))
    rows = db_conn.execute(
        text("""
            SELECT c.symbol, s.session_date, count(*) FROM option_snapshot s
            JOIN option_contracts c USING (contract)
            WHERE c.symbol = ANY(:syms)
            GROUP BY 1, 2
        """),
        {"syms": symbols},
    ).all()
    assert len(rows) == len(symbols) * len(SESSIONS)
    assert all(12 <= n <= 40 for _, _, n in rows)
    total_contracts = db_conn.execute(
        text("SELECT count(*) FROM option_contracts WHERE symbol = ANY(:syms)"),
        {"syms": symbols},
    ).scalar_one()
    assert total_contracts > sum(n for _, _, n in rows) / len(SESSIONS)


def test_first_seen_is_kept_from_the_first_session(db_conn: Connection) -> None:
    _pull_both(db_conn, "C")
    symbols = sorted(_closes("C"))
    seen = {
        r[0]
        for r in db_conn.execute(
            text("SELECT DISTINCT first_seen FROM option_contracts WHERE symbol = ANY(:syms)"),
            {"syms": symbols},
        )
    }
    assert SESSIONS[0] in seen
    contracts_in_both = db_conn.execute(
        text("""
            SELECT c.first_seen FROM option_contracts c
            WHERE c.symbol = ANY(:syms) AND (
                SELECT count(*) FROM option_snapshot s WHERE s.contract = c.contract
            ) = 2
        """),
        {"syms": symbols},
    ).all()
    assert contracts_in_both
    assert all(r[0] == SESSIONS[0] for r in contracts_in_both)


def test_index_daily_has_history_and_todays_row(db_conn: Connection) -> None:
    _pull_both(db_conn, "D")
    indices = _indices("D")
    for symbol in indices:
        n = db_conn.execute(
            text("SELECT count(*) FROM index_daily WHERE symbol = :s"), {"s": symbol}
        ).scalar_one()
        assert n >= 300
    today = db_conn.execute(
        text("SELECT c FROM index_daily WHERE symbol = :s AND session_date = :d"),
        {"s": indices[0], "d": SESSIONS[1]},
    ).scalar_one()
    provider = SyntheticOptionsProvider(_closes("D"), SESSIONS[1])
    assert today == provider.index_quote(indices[0])["data"]["close"]
    quote_rows = db_conn.execute(
        text("""
            SELECT symbol, body FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'delayed_quotes/index'
              AND symbol = ANY(:vendor)
        """),
        {"vendor": ["_" + i for i in indices]},
    ).all()
    assert {r[0] for r in quote_rows} == {"_" + i for i in indices}
    assert all("options" not in r[1]["data"] for r in quote_rows)


def test_pulling_twice_is_idempotent(db_conn: Connection) -> None:
    _pull_both(db_conn, "E")
    before = _counts(db_conn, "E")
    snapshots = _snapshots(db_conn, "E")
    _pull_both(db_conn, "E")
    assert _counts(db_conn, "E") == before
    assert _snapshots(db_conn, "E") == snapshots


def test_rebuild_restores_identical_snapshots_with_no_network(db_conn: Connection) -> None:
    _pull_both(db_conn, "F")
    expected = _snapshots(db_conn, "F")
    db_conn.execute(
        text("""
            DELETE FROM option_snapshot s USING option_contracts c
            WHERE s.contract = c.contract AND c.symbol = ANY(:syms)
        """),
        {"syms": sorted(_closes("F"))},
    )

    def _no_network(*_: object, **__: object) -> None:
        raise AssertionError("rebuild_options touched a provider")

    with (
        patch.object(httpx.Client, "send", _no_network),
        patch.object(SyntheticOptionsProvider, "chain", _no_network),
        patch.object(SyntheticOptionsProvider, "index_quote", _no_network),
        patch.object(SyntheticOptionsProvider, "index_history", _no_network),
    ):
        written = sum(rebuild_options(db_conn, session) for session in SESSIONS)

    # rebuild replays every chain stored for the session, including any other
    # symbol's, so the count is a floor here; the rows themselves are exact.
    assert written >= len(expected)
    assert _snapshots(db_conn, "F") == expected


def test_first_capture_wins_on_a_changed_re_pull(db_conn: Connection) -> None:
    session = SESSIONS[0]
    closes = _closes("G")
    symbols = sorted(closes)
    pull_options(db_conn, SyntheticOptionsProvider(closes, session), symbols, session, indices=())
    first = _snapshots(db_conn, "G")
    moved = {s: c + Decimal("3.00") for s, c in closes.items()}
    result = pull_options(
        db_conn, SyntheticOptionsProvider(moved, session), symbols, session, indices=()
    )
    assert result.symbols_ok == tuple(symbols)
    assert _snapshots(db_conn, "G") == first

    db_conn.execute(
        text("""
            DELETE FROM option_snapshot s USING option_contracts c
            WHERE s.contract = c.contract AND c.symbol = ANY(:syms)
        """),
        {"syms": symbols},
    )
    rebuild_options(db_conn, session)
    assert _snapshots(db_conn, "G") == first


def test_history_replaces_the_provisional_quote_row(db_conn: Connection) -> None:
    session = SESSIONS[1]
    provisional = previous_session(session)
    index = _indices("H")[0]
    db_conn.execute(
        text("INSERT INTO index_daily (symbol, session_date, c) VALUES (:s, :d, 99)"),
        {"s": index, "d": provisional},
    )
    provider = SyntheticOptionsProvider(_closes("H"), session)
    result = pull_options(db_conn, provider, [], session, indices=(index,))
    assert result.indices_ok == (index,)

    line = next(
        ln
        for ln in provider.index_history(index).splitlines()
        if ln.startswith(f"{provisional:%m/%d/%Y},")
    )
    level = Decimal(line.split(",")[4])
    row = db_conn.execute(
        text("SELECT o, h, l, c FROM index_daily WHERE symbol = :s AND session_date = :d"),
        {"s": index, "d": provisional},
    ).one()
    assert tuple(row) == (level, level, level, level)
    body = db_conn.execute(
        text("""
            SELECT body FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'us_indices/daily_prices'
              AND symbol = :s AND as_of = :d
        """),
        {"s": index, "d": session},
    ).scalar_one()
    assert line in body["rows"]
