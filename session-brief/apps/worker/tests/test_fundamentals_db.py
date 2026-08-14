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

from worker.fundamentals import ingest_fundamentals, replay_fundamentals

_SYM = "ZZEDG"


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
    ingest_fundamentals(db_conn, _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

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
    ingest_fundamentals(db_conn, _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))
    before = _count(db_conn)
    ingest_fundamentals(db_conn, _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

    assert _count(db_conn) == before


def test_replay_reproduces_the_table_with_zero_network_calls(db_conn: Connection) -> None:
    """The M2 property. Re-fetching several MB per symbol to fix a concept
    mapping is the mistake this makes impossible."""
    ingest_fundamentals(db_conn, _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))
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
        db_conn, _StubEdgar(), ["ZZUNKNOWN"], as_of=date(2026, 8, 14)
    )

    assert result["skipped"] == ["ZZUNKNOWN"]
    assert db_conn.execute(text(
        "SELECT count(*) FROM fundamentals WHERE symbol = 'ZZUNKNOWN'"
    ).bindparams()).scalar_one() == 0


def test_a_restatement_adds_a_row_rather_than_mutating_one(db_conn: Connection) -> None:
    """Point-in-time history is append-only: the original filing's figure stays
    readable as of its own date."""
    ingest_fundamentals(db_conn, _StubEdgar(), [_SYM], as_of=date(2026, 8, 14))

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

    ingest_fundamentals(db_conn, _Amended(), [_SYM], as_of=date(2026, 9, 20))

    rows = db_conn.execute(text(
        "SELECT as_of, net_income_cents FROM fundamentals WHERE symbol = :s ORDER BY as_of"
    ), {"s": _SYM}).all()

    assert [(r[0], r[1]) for r in rows] == [
        (date(2026, 7, 31), 50000),
        (date(2026, 9, 15), 77700),
    ]
