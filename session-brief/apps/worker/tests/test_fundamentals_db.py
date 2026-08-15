"""M18 against a real database (skipped without DATABASE_URL).

The property only provable here: wiping `fundamentals` and re-normalizing from
`raw_payloads` reproduces the table with zero network calls — the M2 replay
guarantee, applied to a several-MB-per-symbol feed where re-fetching is the
expensive mistake.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.fundamentals import (
    fetch_company_facts,
    ingest_fundamentals,
    replay_fundamentals,
    stale_symbols,
    store_company_facts,
)

_SYM = "ZZEDG"


class _FakeEngine:
    """Hands the test's own (rolled-back) connection to code that expects an
    engine, so per-symbol transactions still leave no trace."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def begin(self) -> Any:
        import contextlib

        return contextlib.nullcontext(self._conn)


class _StubEdgar:
    """Stands in for EdgarClient. Counts calls so the replay test can assert
    zero of them."""

    calls = 0

    def cik_for(self, symbol: str) -> str | None:
        return None if symbol == "ZZUNKNOWN" else "0000000042"

    def company_facts(self, cik: str) -> dict[str, Any]:
        type(self).calls += 1
        return {
            "cik": 42,
            "facts": {
                "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    {"end": "2026-07-17", "val": 1000000, "accn": "a1", "fy": 2026,
                     "fp": "Q3", "form": "10-Q", "filed": "2026-07-31"},
                ]}}},
                "us-gaap": {
                    "NetIncomeLoss": {"units": {"USD": [
                        {"start": "2026-03-29", "end": "2026-06-27", "val": 500,
                         "accn": "a1", "fy": 2026, "fp": "Q3", "form": "10-Q",
                         "filed": "2026-07-31"},
                    ]}},
                    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                        {"end": "2026-06-27", "val": 900, "accn": "a1", "fy": 2026,
                         "fp": "Q3", "form": "10-Q", "filed": "2026-07-31"},
                    ]}},
                },
            },
        }

    def domicile(self, cik: str) -> str | None:
        return "DE"


def _count(conn: Connection) -> int:
    return int(conn.execute(
        text("SELECT count(*) FROM fundamentals WHERE symbol = :s"), {"s": _SYM}
    ).scalar_one())


def test_ingest_writes_typed_rows_and_a_verbatim_payload(db_conn: Connection) -> None:
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    row = db_conn.execute(text(
        "SELECT as_of, period_end, fiscal_period, net_income_cents, cash_cents, "
        "shares_out, domicile, cik, source FROM fundamentals WHERE symbol = :s"
    ), {"s": _SYM}).mappings().one()

    assert row["as_of"] == date(2026, 7, 31)      # filed, not period end
    assert row["period_end"] == date(2026, 6, 27)
    assert row["net_income_cents"] == 50000
    assert row["cash_cents"] == 90000
    assert row["shares_out"] == 1000000
    assert row["domicile"] == "DE"
    assert row["cik"] == "0000000042"
    assert row["source"] == "edgar"

    payloads = db_conn.execute(text(
        "SELECT count(*) FROM raw_payloads WHERE source = 'edgar' AND symbol = :s"
    ), {"s": _SYM}).scalar_one()
    assert payloads == 1


def test_ingesting_twice_does_not_duplicate(db_conn: Connection) -> None:
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))
    before = _count(db_conn)
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    assert _count(db_conn) == before


def test_replay_reproduces_the_table_with_zero_network_calls(db_conn: Connection) -> None:
    """The M2 property. Re-fetching several MB per symbol to fix a concept
    mapping is the mistake this makes impossible."""
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))
    original = db_conn.execute(text(
        "SELECT symbol, as_of, period_end, net_income_cents, cash_cents, shares_out "
        "FROM fundamentals WHERE symbol = :s ORDER BY as_of"
    ), {"s": _SYM}).all()

    db_conn.execute(text("DELETE FROM fundamentals WHERE symbol = :s"), {"s": _SYM})
    _StubEdgar.calls = 0

    replayed = replay_fundamentals(db_conn, [_SYM])

    assert _StubEdgar.calls == 0
    assert replayed == len(original)
    assert db_conn.execute(text(
        "SELECT symbol, as_of, period_end, net_income_cents, cash_cents, shares_out "
        "FROM fundamentals WHERE symbol = :s ORDER BY as_of"
    ), {"s": _SYM}).all() == original


def test_a_symbol_edgar_does_not_know_is_skipped_not_guessed(db_conn: Connection) -> None:
    result = ingest_fundamentals(
        _FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), ["ZZUNKNOWN"], as_of=date(2026, 8, 14),
    )

    assert result["skipped"] == ["ZZUNKNOWN"]
    assert db_conn.execute(text(
        "SELECT count(*) FROM fundamentals WHERE symbol = 'ZZUNKNOWN'"
    )).scalar_one() == 0


# --- Transaction scope ------------------------------------------------------


def test_no_network_call_happens_inside_a_database_transaction(
    db_conn: Connection,
) -> None:
    """Holding a Postgres connection open across HTTP fetches is invisible at
    two symbols and a pooler-exhausting idle-in-transaction at fifty. Every
    fetch must complete before its write opens a transaction."""
    open_transactions = 0
    fetched_inside: list[str] = []

    class _TrackingEngine(_FakeEngine):
        def begin(self) -> Any:
            import contextlib

            @contextlib.contextmanager
            def _tracked() -> Any:
                nonlocal open_transactions
                open_transactions += 1
                try:
                    yield self._conn
                finally:
                    open_transactions -= 1

            return _tracked()

    class _Watching(_StubEdgar):
        def company_facts(self, cik: str) -> dict[str, Any]:
            if open_transactions:
                fetched_inside.append(cik)
            return super().company_facts(cik)

    ingest_fundamentals(
        _TrackingEngine(db_conn),  # type: ignore[arg-type]
        _Watching(), [_SYM, "ZZEDG2"], as_of=date(2026, 8, 14)
    )

    assert fetched_inside == []


# --- Freshness (the staleness assertion the dead-man's switch reads) --------


def test_a_symbol_with_a_recent_filing_is_not_stale(db_conn: Connection) -> None:
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    assert stale_symbols(
        db_conn, [_SYM], as_of=date(2026, 8, 14), max_age_days=120
    ) == []


def test_a_symbol_whose_newest_filing_is_old_is_stale(db_conn: Connection) -> None:
    """A quarter plus filing lag. Past that, an active registrant should have
    filed something — so silence means the pipeline broke, not the company."""
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    assert stale_symbols(
        db_conn, [_SYM], as_of=date(2027, 6, 1), max_age_days=120
    ) == [_SYM]


def test_a_symbol_with_no_rows_at_all_is_stale(db_conn: Connection) -> None:
    """The failure this exists to catch: a run that stores nothing pings
    success exactly like a healthy one."""
    assert stale_symbols(
        db_conn, ["ZZNEVER"], as_of=date(2026, 8, 14), max_age_days=120
    ) == ["ZZNEVER"]


def test_fetch_and_store_are_separable(db_conn: Connection) -> None:
    """The split is what lets the caller keep network out of its transaction."""
    fetched = fetch_company_facts(_StubEdgar(), _SYM)
    assert fetched is not None

    written = store_company_facts(
        db_conn, _SYM, fetched.payload, domicile=fetched.domicile,
        as_of=date(2026, 8, 14),
    )

    assert written == 1
    assert _count(db_conn) == 1


def test_a_restatement_adds_a_row_rather_than_mutating_one(db_conn: Connection) -> None:
    """Point-in-time history is append-only: the original filing's figure stays
    readable as of its own date."""
    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    class _Amended(_StubEdgar):
        def company_facts(self, cik: str) -> dict[str, Any]:
            facts = super().company_facts(cik)
            ni = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]
            facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"] = [
                ni,
                {**ni, "val": 777, "accn": "a2", "form": "10-Q/A",
                 "filed": "2026-09-15"},
            ]
            return facts

    ingest_fundamentals(_FakeEngine(db_conn),  # type: ignore[arg-type]
        _Amended(), [_SYM], as_of=date(2026, 9, 20))

    rows = db_conn.execute(text(
        "SELECT as_of, net_income_cents FROM fundamentals WHERE symbol = :s ORDER BY as_of"
    ), {"s": _SYM}).all()

    assert [(r[0], r[1]) for r in rows] == [
        (date(2026, 7, 31), 50000),
        (date(2026, 9, 15), 77700),
    ]
