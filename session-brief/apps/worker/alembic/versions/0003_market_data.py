"""market data: raw_payloads, bars_daily (M2 ingest + normalize)

Stage ① ingest writes vendor responses verbatim into raw_payloads; stage ②
normalize replays them into bars_daily. These tables carry **no user_id** —
market data is shared across the tenant base and keyed by symbol, which is what
makes ingest cost scale with the symbol universe, not the user count.

Prices are NUMERIC (→ Python Decimal), not integer cents: adjusted closes carry
sub-cent precision and are measurements, not ledger money. The integer-cents
money invariant governs book value / P&L, which are computed downstream.

RLS is enabled with a read-only public policy — market data is shared, non-
sensitive reference data; only the worker (table owner) writes it.

Revision ID: 0003_market_data
Revises: 0002_book
Create Date: 2026-08-10

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_market_data"
down_revision: str | None = "0002_book"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE raw_payloads (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source     text NOT NULL,
            endpoint   text NOT NULL,
            symbol     text NOT NULL,
            as_of      date NOT NULL,
            body       jsonb NOT NULL,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (source, endpoint, symbol, as_of)
        );
        CREATE INDEX raw_payloads_symbol_idx ON raw_payloads (symbol);

        CREATE TABLE bars_daily (
            symbol       text NOT NULL,
            session_date date NOT NULL,
            o            numeric NOT NULL,
            h            numeric NOT NULL,
            l            numeric NOT NULL,
            c            numeric NOT NULL,
            v            bigint  NOT NULL,
            adj_c        numeric NOT NULL,
            PRIMARY KEY (symbol, session_date)
        );
    """)

    # Shared reference data: RLS on (Supabase expects it on public tables), with
    # a read-only policy. Writes go through the worker as table owner, which
    # bypasses RLS — extends D10 to the no-user_id market-data tables.
    op.execute("""
        ALTER TABLE raw_payloads ENABLE ROW LEVEL SECURITY;
        ALTER TABLE bars_daily   ENABLE ROW LEVEL SECURITY;

        CREATE POLICY raw_payloads_read ON raw_payloads FOR SELECT USING (true);
        CREATE POLICY bars_daily_read   ON bars_daily   FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS bars_daily;
        DROP TABLE IF EXISTS raw_payloads;
    """)
