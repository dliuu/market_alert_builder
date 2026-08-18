"""bars_daily: the full split-adjusted series (M19 technical snapshot)

Only `adjClose` was ever stored, so every level drawn from `h`/`l` — swing
pivots, 52-week extremes, ATR — is silently wrong across a split. Over the
252-session window the technical snapshot reads, that is the difference between
a real level and a fiction, and it fails without an error.

Nullable because the columns are backfilled by replay, not by this migration,
and because not every vendor sends them: the CN synthetic feed emits `adjClose`
alone. Downstream must read a null adjusted high as absent history, never zero.

`raw_payloads` already holds these fields on every Tiingo response, so history
is repopulated by `normalize_bars` at zero API cost — the property D13 exists to
guarantee. Run `uv run -m worker.cli normalize` after upgrading.

Revision ID: 0019_bars_adjusted
Revises: 0018_claims_market
Create Date: 2026-08-18

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_bars_adjusted"
down_revision: str | None = "0018_claims_market"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE bars_daily
            ADD COLUMN adj_o numeric,
            ADD COLUMN adj_h numeric,
            ADD COLUMN adj_l numeric,
            ADD COLUMN adj_v bigint;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE bars_daily
            DROP COLUMN IF EXISTS adj_o,
            DROP COLUMN IF EXISTS adj_h,
            DROP COLUMN IF EXISTS adj_l,
            DROP COLUMN IF EXISTS adj_v;
    """)
