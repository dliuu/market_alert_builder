"""sectors.market: discriminator for multi-book support (US vs CN) (M18)

The book is expanded to hold separate US stocks and Chinese A-shares positions.
Holdings and lots inherit the market through sector_id — only sectors changes.

Revision ID: 0015_cn_book
Revises: 0014_catalysts
Create Date: 2026-08-15

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_cn_book"
down_revision: str | None = "0014_catalysts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sectors
            ADD COLUMN market text NOT NULL DEFAULT 'US'
            CONSTRAINT sectors_market_check CHECK (market IN ('US', 'CN'));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE sectors DROP COLUMN market;
    """)
