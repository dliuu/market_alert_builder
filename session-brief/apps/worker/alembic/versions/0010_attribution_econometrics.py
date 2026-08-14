"""M12 attribution econometrics: MAD salience, capped-LOO storage, index-event
seam, and derived-signal tables. All shared reference data (no user_id), RLS on
with a public read policy — the bars_daily/attribution precedent (0009/D21).

Revision ID: 0010_attribution_econometrics
Revises: 0009_attribution
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_attribution_econometrics"
down_revision: str | None = "0009_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE attribution    ADD COLUMN resid_z numeric;
        ALTER TABLE basket_returns ADD COLUMN weights jsonb;

        CREATE TABLE basket_loo_returns (
            theme_id        uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            excluded_symbol text NOT NULL,
            trade_date      date NOT NULL,
            model_version   integer NOT NULL,
            ret             numeric NOT NULL,
            n_members       integer NOT NULL,
            PRIMARY KEY (theme_id, excluded_symbol, trade_date, model_version)
        );

        CREATE TABLE index_events (
            symbol         text NOT NULL,
            trade_date     date NOT NULL,
            index_key      text NOT NULL,
            effective_from date NOT NULL,
            effective_to   date,
            PRIMARY KEY (symbol, trade_date, index_key)
        );

        CREATE TABLE attribution_signals (
            symbol         text NOT NULL,
            trade_date     date NOT NULL,
            model_version  integer NOT NULL,
            beta_drift_20d numeric,
            resid_momentum numeric,
            rolling_alpha  numeric,
            PRIMARY KEY (symbol, trade_date, model_version)
        );

        CREATE TABLE theme_dispersion (
            theme_id       uuid NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            trade_date     date NOT NULL,
            model_version  integer NOT NULL,
            dispersion_mad numeric NOT NULL,
            PRIMARY KEY (theme_id, trade_date, model_version)
        );
    """)

    op.execute("""
        ALTER TABLE basket_loo_returns   ENABLE ROW LEVEL SECURITY;
        ALTER TABLE index_events         ENABLE ROW LEVEL SECURITY;
        ALTER TABLE attribution_signals  ENABLE ROW LEVEL SECURITY;
        ALTER TABLE theme_dispersion     ENABLE ROW LEVEL SECURITY;

        CREATE POLICY basket_loo_returns_read  ON basket_loo_returns  FOR SELECT USING (true);
        CREATE POLICY index_events_read        ON index_events        FOR SELECT USING (true);
        CREATE POLICY attribution_signals_read ON attribution_signals FOR SELECT USING (true);
        CREATE POLICY theme_dispersion_read    ON theme_dispersion    FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS theme_dispersion;
        DROP TABLE IF EXISTS attribution_signals;
        DROP TABLE IF EXISTS index_events;
        DROP TABLE IF EXISTS basket_loo_returns;
        ALTER TABLE basket_returns DROP COLUMN IF EXISTS weights;
        ALTER TABLE attribution    DROP COLUMN IF EXISTS resid_z;
    """)
