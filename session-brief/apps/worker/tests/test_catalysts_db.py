"""M17 catalysts against a real database (skipped without DATABASE_URL).

The two properties that are only provable here: a signal rebuild costs zero API
calls and leaves reporting state untouched (D30), and the decay curve advances
across sessions because it is stored, not recomputed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.catalysts import (
    CatalystSignal,
    InsiderTx,
    mark_reported,
    read_catalysts,
    rebuild_signals,
    store_insider_txs,
    store_signals,
)

_USER = "00000000-0000-0000-0000-0000000000ca"
_SYM = "ZZCAT"
_D = date(2099, 3, 4)
_MV = "test-1"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-catalysts@example.invalid')"),
        {"u": _USER},
    )


def _signal(kind: str = "cluster", severity: int = 4) -> CatalystSignal:
    return CatalystSignal(
        source="insider",
        symbol=_SYM,
        kind=kind,
        ref_date=_D,
        severity=severity,
        detail={"insider_count": 3, "total_value_cents": 1_420_000_000},
        member_ids=(1, 2, 3),
    )


def _tx(row_id: int, name: str, on: date = _D) -> InsiderTx:
    return InsiderTx(
        symbol=_SYM,
        insider_name=name,
        insider_title=None,
        transaction_date=on,
        filing_date=on,
        transaction_code="S",
        shares=Decimal("1000"),
        value_cents=5_000_000,
        shares_after=Decimal("9000"),
        row_id=row_id,
    )


def test_storing_the_same_signal_twice_is_idempotent(db_conn: Connection) -> None:
    assert store_signals(db_conn, [_signal()], model_version=_MV) == 1
    store_signals(db_conn, [_signal()], model_version=_MV)

    count = db_conn.execute(
        text("SELECT count(*) FROM catalyst_signals WHERE symbol = :s"), {"s": _SYM}
    ).scalar_one()
    assert count == 1


def test_a_stored_signal_comes_back_full_the_first_time(db_conn: Connection) -> None:
    _seed_user(db_conn)
    store_signals(db_conn, [_signal()], model_version=_MV)

    items = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)

    assert [(i.kind, i.tier) for i in items] == [("cluster", "full")]
    assert items[0].detail["insider_count"] == 3


def test_the_same_signal_decays_across_three_sessions(db_conn: Connection) -> None:
    """Full -> condensed -> gone. The curve advances only because reporting
    state is written back; recomputing from the signal alone could never do it."""
    _seed_user(db_conn)
    store_signals(db_conn, [_signal()], model_version=_MV)
    seen: list[str | None] = []

    for _ in range(3):
        items = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)
        seen.append(items[0].tier if items else None)
        mark_reported(db_conn, _USER, items, now=datetime.now(UTC))

    assert seen == ["full", "brief", None]


def test_a_signal_that_worsens_re_escalates_to_full(db_conn: Connection) -> None:
    _seed_user(db_conn)
    store_signals(db_conn, [_signal(severity=3)], model_version=_MV)
    for _ in range(2):
        items = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)
        mark_reported(db_conn, _USER, items, now=datetime.now(UTC))

    assert read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV) == []

    # Same signal, now worse: a 2-insider cluster became a 4-insider cluster.
    store_signals(db_conn, [_signal(severity=5)], model_version=_MV)
    items = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)

    assert [i.tier for i in items] == ["full"]


def test_two_users_decay_independently(db_conn: Connection) -> None:
    """Reporting state carries user_id precisely so user #2's first brief does
    not open at 'condensed' because user #1 already read it (D30)."""
    other = "00000000-0000-0000-0000-0000000000cb"
    _seed_user(db_conn)
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-catalysts2@example.invalid')"),
        {"u": other},
    )
    store_signals(db_conn, [_signal()], model_version=_MV)

    first = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)
    mark_reported(db_conn, _USER, first, now=datetime.now(UTC))

    assert [i.tier for i in read_catalysts(db_conn, other, [_SYM], _D, model_version=_MV)] == [
        "full"
    ]


def test_rebuilding_signals_preserves_reporting_state(db_conn: Connection) -> None:
    """The load-bearing regression test. If reporting state were a column on
    catalyst_signals, a rebuild would wipe it and every stale cluster would
    resurface at full volume on the next brief."""
    _seed_user(db_conn)
    store_insider_txs(db_conn, [_tx(0, n) for n in ("A", "B", "C")])
    rebuild_signals(db_conn, model_version=_MV)

    first = read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)
    assert [i.tier for i in first] == ["full"]
    mark_reported(db_conn, _USER, first, now=datetime.now(UTC))

    rebuild_signals(db_conn, model_version=_MV)

    assert [i.tier for i in read_catalysts(db_conn, _USER, [_SYM], _D, model_version=_MV)] == [
        "brief"
    ]


def test_rebuild_reproduces_the_same_signals_from_raw_rows(db_conn: Connection) -> None:
    """'Zero API calls' — the detectors read only the raw tables."""
    store_insider_txs(db_conn, [_tx(0, n) for n in ("A", "B", "C")])

    rebuild_signals(db_conn, model_version=_MV)
    first = db_conn.execute(
        text("SELECT source, kind, ref_date, severity FROM catalyst_signals ORDER BY kind")
    ).all()
    rebuild_signals(db_conn, model_version=_MV)
    second = db_conn.execute(
        text("SELECT source, kind, ref_date, severity FROM catalyst_signals ORDER BY kind")
    ).all()

    assert first == second
    assert ("insider", "cluster", _D, 4) in [tuple(r) for r in first]


def test_ingesting_the_same_filing_twice_inserts_one_row(db_conn: Connection) -> None:
    store_insider_txs(db_conn, [_tx(0, "A")])
    store_insider_txs(db_conn, [_tx(0, "A")])

    count = db_conn.execute(
        text("SELECT count(*) FROM catalyst_insider_tx WHERE symbol = :s"), {"s": _SYM}
    ).scalar_one()
    assert count == 1


def test_a_backdated_filing_is_still_detected_as_new(db_conn: Connection) -> None:
    """Filings arrive late and out of order, so a high-water date alone would
    silently skip this one."""
    store_insider_txs(db_conn, [_tx(0, "A", on=_D)])
    store_insider_txs(db_conn, [_tx(0, "B", on=date(2099, 2, 1))])

    count = db_conn.execute(
        text("SELECT count(*) FROM catalyst_insider_tx WHERE symbol = :s"), {"s": _SYM}
    ).scalar_one()
    assert count == 2
