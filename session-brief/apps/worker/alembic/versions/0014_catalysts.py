"""catalysts: insider flow, Form 144, signals and reporting state (M17)

Five tables, in three layers (docs/superpowers/specs/2026-08-14-m17-catalysts-design.md).

**Raw** — `catalyst_insider_tx`, `catalyst_proposed_sales`. Typed replays of
vendor payloads, deduped on a `natural_key`. They carry no `payload` column:
verbatim bodies live once, in `raw_payloads` (invariant 5, D13, D29), so these
rebuild from there without a fetch.

**Signals** — `catalyst_signals`, one table across every source rather than one
per source plus a union view (D29). The per-source typed fields live in
`detail`, exactly as `flags` carries nine heterogeneous types in one `payload`.
`member_ids` points back at the raw rows that produced a signal: the audit
trail for drill-down and for debugging false positives. Disposable by design —
`catalysts rebuild` drops and repopulates this table alone.

**Reporting state** — `catalyst_reporting_state`, which must survive that
rebuild. It is the one table here that carries `user_id`: report-once decay is
a property of a reader's attention, so two users holding the same name each get
their own full -> condensed -> suppressed curve. It is keyed on the signal's
natural identity `(source, symbol, kind, ref_date)` and deliberately *not* on
`catalyst_signals.id`, which a rebuild reassigns — the source spec's own
`(source, source_id)` key would orphan every row on the first rebuild and
resurface every stale cluster at full volume.

Everything except reporting state is shared and keyed by symbol, following the
market-data carve-out (docs/03, D21): insider filings are facts about a symbol,
not about a book, so ingest cost scales with the symbol universe.

Revision ID: 0014_catalysts
Revises: 0013_claims_premarket_gap
Create Date: 2026-08-14

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_catalysts"
down_revision: str | None = "0013_claims_premarket_gap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Raw ---------------------------------------------------------------
    # Money is integer cents (invariant); shares and weights stay numeric —
    # they are quantities and ratios, not money.
    op.execute("""
        CREATE TABLE catalyst_insider_tx (
            id               bigserial PRIMARY KEY,
            symbol           text NOT NULL,
            insider_name     text NOT NULL,
            insider_title    text,
            transaction_date date NOT NULL,
            filing_date      date NOT NULL,
            transaction_code text NOT NULL,
            shares           numeric,
            price_cents      bigint,
            value_cents      bigint,
            shares_after     numeric,
            natural_key      text NOT NULL UNIQUE,
            ingested_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX catalyst_insider_tx_filing_idx
            ON catalyst_insider_tx (symbol, filing_date DESC);
        CREATE INDEX catalyst_insider_tx_txn_idx
            ON catalyst_insider_tx (symbol, transaction_date DESC);
    """)

    op.execute("""
        CREATE TABLE catalyst_proposed_sales (
            id               bigserial PRIMARY KEY,
            symbol           text NOT NULL,
            insider_name     text NOT NULL,
            filing_date      date NOT NULL,
            shares_proposed  numeric,
            approx_sale_date date,
            broker           text,
            natural_key      text NOT NULL UNIQUE,
            ingested_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX catalyst_proposed_sales_filing_idx
            ON catalyst_proposed_sales (symbol, filing_date DESC);
    """)

    # Watermarks. `seen_keys` is a rolling window of natural keys, not just a
    # high-water date: filings arrive late and out of order, so a date alone
    # would silently skip a backdated Form 4.
    op.execute("""
        CREATE TABLE catalyst_watermarks (
            source            text NOT NULL,
            symbol            text NOT NULL,
            last_success_at   timestamptz,
            last_seen_date    date,
            seen_keys         text[] NOT NULL DEFAULT '{}',
            last_error        text,
            consecutive_fails int NOT NULL DEFAULT 0,
            PRIMARY KEY (source, symbol)
        );
    """)

    # --- Signals -----------------------------------------------------------
    op.execute("""
        CREATE TABLE catalyst_signals (
            id            bigserial PRIMARY KEY,
            source        text NOT NULL,
            symbol        text NOT NULL,
            kind          text NOT NULL,
            ref_date      date NOT NULL,
            severity      smallint NOT NULL,
            detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
            member_ids    bigint[] NOT NULL DEFAULT '{}',
            model_version text NOT NULL,
            computed_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT catalyst_signals_source_check CHECK (
                source IN ('insider', 'proposed', 'index', 'etf', 'lockup', 'eligibility')
            ),
            CONSTRAINT catalyst_signals_severity_check CHECK (
                severity BETWEEN 1 AND 5
            ),
            CONSTRAINT catalyst_signals_natural_key
                UNIQUE (source, symbol, kind, ref_date, model_version)
        );
        CREATE INDEX catalyst_signals_symbol_idx
            ON catalyst_signals (symbol, ref_date DESC);
    """)

    # --- Reporting state ---------------------------------------------------
    op.execute("""
        CREATE TABLE catalyst_reporting_state (
            user_id           uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            source            text NOT NULL,
            symbol            text NOT NULL,
            kind              text NOT NULL,
            ref_date          date NOT NULL,
            first_reported_at timestamptz NOT NULL,
            last_reported_at  timestamptz NOT NULL,
            report_count      int NOT NULL DEFAULT 1,
            max_severity_seen smallint NOT NULL,
            PRIMARY KEY (user_id, source, symbol, kind, ref_date)
        );
    """)

    # Shared reference tables get RLS with a read-only policy (the
    # 0003_market_data / 0012_quotes pattern); the per-user one is scoped by
    # auth.uid() like the book tables.
    op.execute("""
        ALTER TABLE catalyst_insider_tx ENABLE ROW LEVEL SECURITY;
        CREATE POLICY catalyst_insider_tx_read ON catalyst_insider_tx
            FOR SELECT USING (true);
        ALTER TABLE catalyst_proposed_sales ENABLE ROW LEVEL SECURITY;
        CREATE POLICY catalyst_proposed_sales_read ON catalyst_proposed_sales
            FOR SELECT USING (true);
        ALTER TABLE catalyst_signals ENABLE ROW LEVEL SECURITY;
        CREATE POLICY catalyst_signals_read ON catalyst_signals
            FOR SELECT USING (true);
        ALTER TABLE catalyst_reporting_state ENABLE ROW LEVEL SECURITY;
        CREATE POLICY catalyst_reporting_state_own ON catalyst_reporting_state
            FOR ALL USING (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS catalyst_reporting_state;
        DROP TABLE IF EXISTS catalyst_signals;
        DROP TABLE IF EXISTS catalyst_watermarks;
        DROP TABLE IF EXISTS catalyst_proposed_sales;
        DROP TABLE IF EXISTS catalyst_insider_tx;
    """)
