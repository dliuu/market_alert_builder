"""briefs.kind: widen the CHECK to accept the CN kinds (CN-M1, D32)

Contract v7 adds `open_cn`/`close_cn` alongside `open`/`close`. The briefs
table's kind CHECK (named by Postgres's default convention, verified against
0005_briefs) must widen to match or every CN brief write fails at the DB layer
before it ever reaches the contract.

Revision ID: 0016_cn_brief_kinds
Revises: 0015_cn_book
Create Date: 2026-08-15

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_cn_brief_kinds"
down_revision: str | None = "0015_cn_book"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE briefs DROP CONSTRAINT briefs_kind_check;
        ALTER TABLE briefs ADD CONSTRAINT briefs_kind_check
            CHECK (kind IN ('open', 'close', 'open_cn', 'close_cn'));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE briefs DROP CONSTRAINT briefs_kind_check;
        ALTER TABLE briefs ADD CONSTRAINT briefs_kind_check
            CHECK (kind IN ('open', 'close'));
    """)
