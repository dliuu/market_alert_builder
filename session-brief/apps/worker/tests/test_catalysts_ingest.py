"""M17 ingest: provider -> raw_payloads (verbatim) -> typed catalyst tables.

The provider seam is a third protocol, not a widened MarketDataProvider — M15
proved that breaks `mypy --strict` because protocols are structural and the EOD
provider cannot serve these endpoints (D28, D30).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.catalysts_ingest import ingest_catalysts, normalize_insider
from worker.providers.synthetic import SyntheticCatalystProvider

_D = date(2099, 4, 6)


def test_the_synthetic_provider_is_deterministic() -> None:
    """A pure function of (symbol, date) by hash, never random — the M15
    determinism contract, which is what makes a seeded fixture snapshottable."""
    a = SyntheticCatalystProvider(_D).insider_transactions("SNDK")
    b = SyntheticCatalystProvider(_D).insider_transactions("SNDK")

    assert a == b
    assert a != SyntheticCatalystProvider(_D).insider_transactions("RKLB")


def test_the_synthetic_provider_emits_the_documented_shape() -> None:
    rows = SyntheticCatalystProvider(_D).insider_transactions("SNDK")

    assert rows, "a seeded symbol must produce filings or the section can't be developed"
    row = rows[0]
    assert {"symbol", "insider_name", "transaction_date", "filing_date",
            "transaction_type", "shares", "price", "shares_after"} <= set(row)


def test_normalize_maps_vendor_types_onto_form_4_codes() -> None:
    txs = normalize_insider([{
        "symbol": "SNDK", "insider_name": "Jane Roe", "insider_title": "CFO",
        "transaction_date": "2099-04-06", "filing_date": "2099-04-06",
        "transaction_type": "Sale", "shares": "1000", "price": "50.00",
        "shares_after": "9000",
    }])

    assert txs[0].transaction_code == "S"
    assert txs[0].value_cents == 5_000_000  # 1000 * $50, integer cents
    assert txs[0].shares_after == Decimal("9000")


def test_an_unrecognised_vendor_type_becomes_the_ambiguous_code() -> None:
    """Open question 2 in the data: we record that we could not classify it
    rather than guessing a direction."""
    txs = normalize_insider([{
        "symbol": "SNDK", "insider_name": "Jane Roe", "insider_title": None,
        "transaction_date": "2099-04-06", "filing_date": "2099-04-06",
        "transaction_type": "Something New", "shares": "1000", "price": "50.00",
        "shares_after": None,
    }])

    assert txs[0].transaction_code == "?"


def test_normalize_keeps_money_off_the_float_path() -> None:
    txs = normalize_insider([{
        "symbol": "SNDK", "insider_name": "Jane Roe", "insider_title": None,
        "transaction_date": "2099-04-06", "filing_date": "2099-04-06",
        "transaction_type": "Sale", "shares": "3", "price": "0.10",
        "shares_after": None,
    }])

    assert txs[0].value_cents == 30  # 3 * 10c exactly, not 30.000000000000004


def test_ingest_stores_payloads_verbatim_and_is_idempotent(db_conn: Connection) -> None:
    """Invariant 5: the verbatim body lives once, in raw_payloads, and the
    typed tables replay from it. Re-running the session is a no-op."""
    provider = SyntheticCatalystProvider(_D)

    ingest_catalysts(db_conn, provider, ["ZZING"], as_of=_D)
    ingest_catalysts(db_conn, provider, ["ZZING"], as_of=_D)

    payloads = db_conn.execute(
        text("SELECT count(*) FROM raw_payloads WHERE source = 'fdn' AND symbol = 'ZZING'")
    ).scalar_one()
    typed = db_conn.execute(
        text("SELECT count(*) FROM catalyst_insider_tx WHERE symbol = 'ZZING'")
    ).scalar_one()

    assert payloads >= 1
    assert typed >= 1
    ingest_catalysts(db_conn, provider, ["ZZING"], as_of=_D)
    assert db_conn.execute(
        text("SELECT count(*) FROM catalyst_insider_tx WHERE symbol = 'ZZING'")
    ).scalar_one() == typed


def test_one_symbol_failing_does_not_abort_the_run(db_conn: Connection) -> None:
    """Independent failure domains: a failed pull degrades one line, it does
    not kill the section. The failure is recorded on the watermark."""

    class Flaky(SyntheticCatalystProvider):
        def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, object]]:
            if symbol == "ZZBAD":
                raise RuntimeError("vendor 500")
            return super().insider_transactions(symbol, offset=offset)

    ingest_catalysts(db_conn, Flaky(_D), ["ZZBAD", "ZZGOOD"], as_of=_D)

    assert db_conn.execute(
        text("SELECT count(*) FROM catalyst_insider_tx WHERE symbol = 'ZZGOOD'")
    ).scalar_one() >= 1
    fails = db_conn.execute(
        text("SELECT consecutive_fails, last_error FROM catalyst_watermarks "
             "WHERE source = 'insider' AND symbol = 'ZZBAD'")
    ).mappings().one()
    assert fails["consecutive_fails"] == 1
    assert "vendor 500" in fails["last_error"]


def test_a_recovered_symbol_resets_its_failure_count(db_conn: Connection) -> None:
    class Flaky(SyntheticCatalystProvider):
        broken = True

        def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, object]]:
            if Flaky.broken:
                raise RuntimeError("vendor 500")
            return super().insider_transactions(symbol, offset=offset)

    ingest_catalysts(db_conn, Flaky(_D), ["ZZFLAP"], as_of=_D)
    Flaky.broken = False
    ingest_catalysts(db_conn, Flaky(_D), ["ZZFLAP"], as_of=_D)

    row = db_conn.execute(
        text("SELECT consecutive_fails, last_success_at FROM catalyst_watermarks "
             "WHERE source = 'insider' AND symbol = 'ZZFLAP'")
    ).mappings().one()
    assert row["consecutive_fails"] == 0
    assert row["last_success_at"] is not None
