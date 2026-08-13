"""quotes: the pre-open capture the open brief's §2/§3 read (M15)

`docs/03-data-model.md` has sketched this table since day one, but nothing ever
created it — `0003_market_data` built only raw_payloads and bars_daily, because
everything through M14 runs on daily bars. §2 (overnight macro tape) and §3
(your names, pre-market) are the first readers.

Two deviations from the docs/03 sketch, both deliberate:

- **Keyed `(symbol, session_date)`, not `(symbol, captured_at)`.** Every read is
  "the pre-open capture for session D". A timestamp key would make that a range
  scan and make re-seeding duplicate rather than replace. `captured_at` stays as
  an attribute because §3's header renders it.
- **No `user_id`.** Market data is shared and keyed by symbol (D18/D21), which is
  what keeps ingest cost per-symbol rather than per-user.

Held names land in `extended_last`/`extended_v` (the pre-market print and the
summed pre-market volume); the macro tape symbols land in `last`/`prev_close`.
One table, because both are "a quote for a symbol, captured before the open".

Revision ID: 0011_quotes
Revises: 0010_open_events
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_quotes"
down_revision: str | None = "0010_open_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE quotes (
            symbol        text NOT NULL,
            session_date  date NOT NULL,
            captured_at   timestamptz NOT NULL,
            last          numeric,
            prev_close    numeric,
            extended_last numeric,
            extended_v    bigint,
            PRIMARY KEY (symbol, session_date)
        );
        CREATE INDEX quotes_session_idx ON quotes (session_date);
    """)

    # Shared reference data: RLS on with a read-only policy, exactly as
    # 0003_market_data does for bars_daily. The worker writes as table owner.
    op.execute("""
        ALTER TABLE quotes ENABLE ROW LEVEL SECURITY;
        CREATE POLICY quotes_read ON quotes FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quotes;")
