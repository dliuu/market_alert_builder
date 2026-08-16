"""claims: scope the accountability ledger by market (CN-M4)

`resolve_due_claims` filtered on user_id alone, so the CN close brief — which
fires at 15:20 CST, hours before the US close — would grade and consume the US
book's due claims (the D23(3) trap, cross-market). Market is a stored column
rather than a join through holdings/sectors because a claim outlives the
position it was made about: sell the name and the join drops a claim that is
still owed a grade. Default 'US' backfills every existing row as what it is.

The UNIQUE (user_id, symbol, claim_type, session_date) constraint stands: CN
symbols are exchange-suffixed and cannot collide with US tickers, the same
reasoning holdings UNIQUE(user_id, symbol) rests on (D32). See D34.

Revision ID: 0018_claims_market
Revises: 0017_merge_covers_from_and_cn
Create Date: 2026-08-16

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_claims_market"
down_revision: str | None = "0017_merge_covers_from_and_cn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE claims ADD COLUMN market text NOT NULL DEFAULT 'US';
        ALTER TABLE claims ADD CONSTRAINT claims_market_check
            CHECK (market IN ('US', 'CN'));

        DROP INDEX IF EXISTS claims_unresolved_idx;
        CREATE INDEX claims_unresolved_idx ON claims (user_id, market, session_date)
            WHERE outcome IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM claims WHERE market <> 'US';

        DROP INDEX IF EXISTS claims_unresolved_idx;
        CREATE INDEX claims_unresolved_idx ON claims (user_id, session_date)
            WHERE outcome IS NULL;

        ALTER TABLE claims DROP CONSTRAINT claims_market_check;
        ALTER TABLE claims DROP COLUMN market;
    """)
