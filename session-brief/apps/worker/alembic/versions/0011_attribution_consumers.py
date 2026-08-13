"""M13 attribution consumers: claims grade against the realized residual sign,
and the flags.flag_type CHECK is expanded for Task 7's new flag types (M13).

`claims.graded_model_version` freezes which attribution model version produced
a claim's grade, mirroring `attribution.model_version` (D21) — a future re-spec
never silently rewrites past grades. The M13 spec's "no schema change" for
flags is incorrect: `flag_type` carries a CHECK constraint (0007), so the two
new values (`theme_misfit`, `beta_instability`) need it dropped and re-added
under its existing auto-generated name.

Revision ID: 0011_attribution_consumers
Revises: 0010_attribution_econometrics
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_attribution_consumers"
down_revision: str | None = "0010_attribution_econometrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE claims ADD COLUMN graded_model_version integer;

        ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_flag_type_check;
        ALTER TABLE flags ADD CONSTRAINT flags_flag_type_check
            CHECK (flag_type IN ('concentration', 'correlation', 'runway', 'dilution',
                                 'earnings_soon', 'supply_event', 'short_interest',
                                 'theme_misfit', 'beta_instability'));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_flag_type_check;
        ALTER TABLE flags ADD CONSTRAINT flags_flag_type_check
            CHECK (flag_type IN ('concentration', 'correlation', 'runway', 'dilution',
                                 'earnings_soon', 'supply_event', 'short_interest'));

        ALTER TABLE claims DROP COLUMN IF EXISTS graded_model_version;
    """)
