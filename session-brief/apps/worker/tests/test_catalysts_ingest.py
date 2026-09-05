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

from worker.catalysts_ingest import ingest_catalysts, normalize_insider, normalize_proposed
from worker.providers.synthetic import SyntheticCatalystProvider

_D = date(2099, 4, 6)


def test_the_synthetic_provider_is_deterministic() -> None:
    """A pure function of (symbol, date) by hash, never random — the M15
    determinism contract, which is what makes a seeded fixture snapshottable."""
    a = SyntheticCatalystProvider(_D).insider_transactions("SNDK")
    b = SyntheticCatalystProvider(_D).insider_transactions("SNDK")

    assert a == b
    assert a != SyntheticCatalystProvider(_D).insider_transactions("RKLB")


def test_the_synthetic_provider_emits_the_vendor_shape() -> None:
    rows = SyntheticCatalystProvider(_D).insider_transactions("SNDK")

    assert rows, "a seeded symbol must produce filings or the section can't be developed"
    row = rows[0]
    assert {"trading_symbol", "insider_name", "relationship_to_issuer",
            "transaction_date", "transaction_code", "amount_of_securities",
            "price_per_security", "securities_owned_following_transaction",
            "is_derivatives_transaction"} <= set(row)


def test_normalize_parses_the_vendor_record() -> None:
    """Field names and types are copied from a live 2026-09-04 probe of
    `insider-transactions identifier=ASTS` — the shape is a fact, not a guess."""
    txs = normalize_insider([{
        "trading_symbol": "ASTS", "insider_name": "Cisneros Adriana",
        "relationship_to_issuer": "Director", "transaction_date": "2026-08-31",
        "transaction_code": "P", "amount_of_securities": 8768,
        "price_per_security": Decimal("57.0"), "acquired_or_disposed": "A",
        "securities_owned_following_transaction": 796353,
        "is_derivatives_transaction": False, "ownership_form": "I",
    }])

    t = txs[0]
    assert t.symbol == "ASTS"
    assert t.insider_title == "Director"
    assert t.transaction_code == "P"
    assert t.value_cents == 49_977_600  # 8768 * $57.00, integer cents
    assert t.shares_after == Decimal(796353)
    assert t.filing_date == t.transaction_date  # vendor sends no filing date


def test_normalize_skips_derivative_legs_and_maps_x_to_exercise() -> None:
    """An option exercise arrives as two rows: a derivative leg (skipped —
    counting both would double every exercise) and a non-derivative leg whose
    code X is mechanical, like M — a direction-less transaction."""
    txs = normalize_insider([
        {"trading_symbol": "ASTS", "insider_name": "Yao Huiwen",
         "relationship_to_issuer": "Chief Technology Officer",
         "transaction_date": "2026-08-19", "transaction_code": "X",
         "amount_of_securities": 40000, "price_per_security": Decimal("0.0"),
         "is_derivatives_transaction": True},
        {"trading_symbol": "ASTS", "insider_name": "Yao Huiwen",
         "relationship_to_issuer": "Chief Technology Officer",
         "transaction_date": "2026-08-19", "transaction_code": "X",
         "amount_of_securities": 40000, "price_per_security": Decimal("0.0641"),
         "securities_owned_following_transaction": 74750,
         "is_derivatives_transaction": False},
    ])

    assert len(txs) == 1
    assert txs[0].transaction_code == "M"  # X = exercise, mechanical, no direction


def test_an_unrecognised_vendor_type_becomes_the_ambiguous_code() -> None:
    """Open question 2 in the data: we record that we could not classify it
    rather than guessing a direction."""
    txs = normalize_insider([{
        "trading_symbol": "SNDK", "insider_name": "Jane Roe", "relationship_to_issuer": None,
        "transaction_date": "2099-04-06",
        "transaction_code": "Something New", "amount_of_securities": "1000", "price_per_security": "50.00",
        "securities_owned_following_transaction": None, "is_derivatives_transaction": False,
    }])

    assert txs[0].transaction_code == "?"


def test_normalize_keeps_money_off_the_float_path() -> None:
    txs = normalize_insider([{
        "trading_symbol": "SNDK", "insider_name": "Jane Roe", "relationship_to_issuer": None,
        "transaction_date": "2099-04-06",
        "transaction_code": "S", "amount_of_securities": "3", "price_per_security": "0.10",
        "securities_owned_following_transaction": None, "is_derivatives_transaction": False,
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


def test_normalize_proposed_parses_the_vendor_record() -> None:
    """Live 2026-09-04 probe of `proposed-sales identifier=ASTS`. The vendor
    sends no filing date; the approximate sale date anchors the row, so
    `unconverted_144` reads "past the stated sale date and still unexecuted"."""
    sales = normalize_proposed([{
        "trading_symbol": "ASTS", "seller_name": "AA Gables 2, LLC",
        "relationship_to_issuer": "(1)", "broker_name": "Citigroup Global Markets Inc.",
        "amount_of_securities_to_be_sold": 2500000,
        "market_value": Decimal("182975000.0"),
        "amount_of_securities_outstanding": 298746383,
        "approximate_date_of_sale": "2026-06-22",
        "acquisition_period_start": "2026-06-22", "acquisition_period_end": "2026-06-22",
    }])

    s = sales[0]
    assert s.symbol == "ASTS"
    assert s.insider_name == "AA Gables 2, LLC"
    assert s.shares_proposed == Decimal(2500000)
    assert s.filing_date == date(2026, 6, 22)
    assert s.approx_sale_date == date(2026, 6, 22)


def test_normalize_proposed_skips_a_record_with_no_date() -> None:
    """A 144 that can't be dated can't be keyed, aged, or matched to a Form 4 —
    skipping it is recorded honesty, inventing a date is not."""
    assert normalize_proposed([{
        "trading_symbol": "ASTS", "seller_name": "X",
        "amount_of_securities_to_be_sold": 100,
        "approximate_date_of_sale": None, "acquisition_period_end": None,
    }]) == []
