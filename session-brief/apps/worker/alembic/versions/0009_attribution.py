"""return attribution: themes, PIT members, baskets, fits, decomposition (M11)

All five tables are SHARED (no user_id): the market+theme+idiosyncratic
decomposition of a symbol is a property of the symbol and the theme model,
identical for every holder — the bars_daily/fundamentals/events precedent
(D18/D21), not the user-keyed metrics table (D14). RLS on with a public read
policy; the worker writes as table owner.

Revision ID: 0009_attribution
Revises: 0008_deliveries
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_attribution"
down_revision: str | None = "0008_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE themes (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            key        text NOT NULL UNIQUE,
            name       text NOT NULL,
            sort_order integer NOT NULL DEFAULT 0
        );

        CREATE TABLE theme_members (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            theme_id       uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            symbol         text NOT NULL,
            weight         numeric NOT NULL DEFAULT 1,   -- stored, IGNORED in M11 (M12 capping)
            is_primary     boolean NOT NULL DEFAULT true,
            effective_from date NOT NULL,
            effective_to   date                          -- NULL = still effective
        );
        CREATE INDEX theme_members_lookup_idx
            ON theme_members (theme_id, symbol, effective_from);
        CREATE INDEX theme_members_symbol_idx
            ON theme_members (symbol, effective_from);

        CREATE TABLE basket_returns (
            theme_id      uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            trade_date    date NOT NULL,
            model_version integer NOT NULL,
            ret           numeric NOT NULL,
            n_members     integer NOT NULL,
            synthetic     boolean NOT NULL DEFAULT false,
            revised       boolean NOT NULL DEFAULT false,
            PRIMARY KEY (theme_id, trade_date, model_version)
        );

        CREATE TABLE attribution_fits (
            symbol        text NOT NULL,
            model_version integer NOT NULL,
            fit_date      date NOT NULL,
            window_start  date NOT NULL,
            window_end    date NOT NULL,
            beta_market   numeric NOT NULL,
            beta_theme    numeric NOT NULL,
            alpha         numeric NOT NULL,
            r2            numeric,
            resid_scale   numeric,
            n_obs         integer NOT NULL,
            cold_start    boolean NOT NULL DEFAULT false,
            diagnostics   jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (symbol, model_version, fit_date)
        );

        CREATE TABLE attribution (
            symbol        text NOT NULL,
            trade_date    date NOT NULL,
            model_version integer NOT NULL,
            market_bps    numeric NOT NULL,
            theme_bps     numeric NOT NULL,
            resid_bps     numeric NOT NULL,
            total_bps     numeric NOT NULL,
            beta_market   numeric NOT NULL,
            beta_theme    numeric NOT NULL,
            r2            numeric,
            n_obs         integer NOT NULL,
            provisional   boolean NOT NULL DEFAULT false,
            cold_start    boolean NOT NULL DEFAULT false,
            synthetic     boolean NOT NULL DEFAULT false,
            revised       boolean NOT NULL DEFAULT false,
            computed_at   timestamptz NOT NULL,
            PRIMARY KEY (symbol, trade_date, model_version)
        );
    """)

    # All attribution tables are shared reference data (like bars_daily): RLS on
    # with a read-only public policy; the worker writes as table owner (D10/D21).
    op.execute("""
        ALTER TABLE themes           ENABLE ROW LEVEL SECURITY;
        ALTER TABLE theme_members    ENABLE ROW LEVEL SECURITY;
        ALTER TABLE basket_returns   ENABLE ROW LEVEL SECURITY;
        ALTER TABLE attribution_fits ENABLE ROW LEVEL SECURITY;
        ALTER TABLE attribution      ENABLE ROW LEVEL SECURITY;

        CREATE POLICY themes_read           ON themes           FOR SELECT USING (true);
        CREATE POLICY theme_members_read    ON theme_members    FOR SELECT USING (true);
        CREATE POLICY basket_returns_read   ON basket_returns   FOR SELECT USING (true);
        CREATE POLICY attribution_fits_read ON attribution_fits FOR SELECT USING (true);
        CREATE POLICY attribution_read      ON attribution      FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS attribution;
        DROP TABLE IF EXISTS attribution_fits;
        DROP TABLE IF EXISTS basket_returns;
        DROP TABLE IF EXISTS theme_members;
        DROP TABLE IF EXISTS themes;
    """)
