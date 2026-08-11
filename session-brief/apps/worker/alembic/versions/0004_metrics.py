"""metrics: per-position returns, P&L, contribution (M3 compute)

Stage ③ compute writes per-symbol metrics here in long format. The table
carries user_id (invariant 4) — contribution/weight/P&L are book-specific, so a
metric is only meaningful within a user's book. This reconciles the data-model
sketch (which omitted user_id) with the every-table-has-user_id rule; see D14.

Units are carried by the metric name: `*_cents` are integer cents, `*_bps` are
basis points, `day_return`/`weight` are fractions. RLS keys on user_id, like the
book tables (D7).

Revision ID: 0004_metrics
Revises: 0003_market_data
Create Date: 2026-08-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_metrics"
down_revision: str | None = "0003_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE metrics (
            user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol       text NOT NULL,
            session_date date NOT NULL,
            metric       text NOT NULL,
            value        numeric NOT NULL,
            PRIMARY KEY (user_id, symbol, session_date, metric)
        );
        CREATE INDEX metrics_user_date_idx ON metrics (user_id, session_date);

        ALTER TABLE metrics ENABLE ROW LEVEL SECURITY;
        CREATE POLICY metrics_tenant ON metrics
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS metrics;")
