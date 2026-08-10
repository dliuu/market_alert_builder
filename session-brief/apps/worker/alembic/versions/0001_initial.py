"""initial empty baseline

Establishes the migration chain. The book tables (users, sectors, holdings,
lots) land in M1; market-data and derived tables follow.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-10

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
