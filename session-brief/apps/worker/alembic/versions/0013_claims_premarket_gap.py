"""claims: admit the horizon-0 morning claim (M15)

Task 2 widened the contract (`brief-object.schema.json`) to a `premarket_gap`
claim type and a `horizon_sessions` minimum of 0, but the DB owns the schema
(invariant 1) and its own CHECK constraints from 0006_claims were never moved
with it: `claim_type` didn't admit `premarket_gap` and `horizon_sessions`
required `>= 1`. Task 8 needs both to store the open brief's same-day claim.

Revision ID: 0013_claims_premarket_gap
Revises: 0012_quotes
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_claims_premarket_gap"
down_revision: str | None = "0012_quotes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE claims DROP CONSTRAINT claims_claim_type_check;
        ALTER TABLE claims ADD CONSTRAINT claims_claim_type_check
            CHECK (claim_type IN ('catalyst_pending', 'relative_strength',
                                  'supply_overhang', 'breadth', 'premarket_gap'));

        ALTER TABLE claims DROP CONSTRAINT claims_horizon_sessions_check;
        ALTER TABLE claims ADD CONSTRAINT claims_horizon_sessions_check
            CHECK (horizon_sessions >= 0);
    """)


def downgrade() -> None:
    # Rows the old shape can't hold have to go before the constraints come back.
    op.execute("""
        DELETE FROM claims WHERE claim_type = 'premarket_gap' OR horizon_sessions = 0;

        ALTER TABLE claims DROP CONSTRAINT claims_horizon_sessions_check;
        ALTER TABLE claims ADD CONSTRAINT claims_horizon_sessions_check
            CHECK (horizon_sessions >= 1);

        ALTER TABLE claims DROP CONSTRAINT claims_claim_type_check;
        ALTER TABLE claims ADD CONSTRAINT claims_claim_type_check
            CHECK (claim_type IN ('catalyst_pending', 'relative_strength',
                                  'supply_overhang', 'breadth'));
    """)
