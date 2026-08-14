"""edgar fundamentals: give the table a source, and a point-in-time grain (M18)

`fundamentals` has held zero rows since 0007_flags created it — the only writer
was a dry-run script that rolls back — so M7's runway/dilution flags, M17's
`large_144`, and three of M19's five eligibility criteria have all been reading
an empty table. This migration is the schema half of wiring SEC EDGAR
`companyfacts` to it.

Two changes, both forced by the real API shape rather than chosen:

**New columns.** `period_end` / `fiscal_period` separate *when a fact was
filed* from *what period it describes*; `net_income_cents` and `public_float_cents`
are what M19's eligibility engine needs; `domicile` comes from the `submissions`
endpoint (it is a string fact and so absent from `companyfacts` entirely);
`cik` is EDGAR's real key, and `source` keeps ingested rows distinguishable from
the synthetic ones dry-run scripts write.

**The primary key gains `period_end`.** `(symbol, as_of)` is not unique in live
data: Apple's 2010-01-25 filing carries restated facts for two different periods
under two accession numbers, both with that filing date. Verified against a live
`companyfacts` response, not assumed. The honest grain for a point-in-time store
is "what we learned on this date about this period", which is exactly
`(symbol, as_of, period_end)`.

`as_of` remains the **filing** date and never the period end (D31): a Q2 fact is
not knowable on the last day of Q2, and AAPL's own `EntityPublicFloat` for
period end 2025-03-28 was not filed until 2025-10-31 — seven months of
look-ahead if keyed the other way.

The table is empty in every environment, so no backfill is required and
`period_end` can be NOT NULL from the start.

Revision ID: 0015_edgar_fundamentals
Revises: 0014_catalysts
Create Date: 2026-08-14

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_edgar_fundamentals"
down_revision: str | None = "0014_catalysts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Defaulted so the two existing insert sites (a dry-run script and a flags
    # test) keep working; real ingest always supplies a period_end.
    op.execute("""
        ALTER TABLE fundamentals
            ADD COLUMN period_end         date NOT NULL DEFAULT '1900-01-01',
            ADD COLUMN fiscal_period      text,
            ADD COLUMN net_income_cents   bigint,
            ADD COLUMN public_float_cents bigint,
            ADD COLUMN domicile           text,
            ADD COLUMN cik                text,
            ADD COLUMN source             text NOT NULL DEFAULT 'edgar';
    """)

    op.execute("""
        ALTER TABLE fundamentals DROP CONSTRAINT fundamentals_pkey;
        ALTER TABLE fundamentals ADD PRIMARY KEY (symbol, as_of, period_end);
    """)

    op.execute("""
        CREATE INDEX fundamentals_period_idx ON fundamentals (symbol, period_end DESC);
        CREATE INDEX fundamentals_cik_idx ON fundamentals (cik);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS fundamentals_cik_idx;
        DROP INDEX IF EXISTS fundamentals_period_idx;
        ALTER TABLE fundamentals DROP CONSTRAINT fundamentals_pkey;
        ALTER TABLE fundamentals ADD PRIMARY KEY (symbol, as_of);
        ALTER TABLE fundamentals
            DROP COLUMN period_end,
            DROP COLUMN fiscal_period,
            DROP COLUMN net_income_cents,
            DROP COLUMN public_float_cents,
            DROP COLUMN domicile,
            DROP COLUMN cik,
            DROP COLUMN source;
    """)
