"""claims: the accountability loop — emitted predictions and their outcomes (M6)

A brief emits falsifiable claims (docs/05); a later brief grades them from the
tape and writes back `outcome` + `resolved_session`. `session_date` is the emit
session (makes resolution a date query, not a join); `brief_id` is the object's
text slug (deterministic, available before the briefs row is written), not a FK
to briefs.id. UNIQUE (user_id, symbol, claim_type, session_date) makes emission
idempotent on re-assembly. RLS on user_id (D7/D10). See D17.

Revision ID: 0006_claims
Revises: 0005_briefs
Create Date: 2026-08-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_claims"
down_revision: str | None = "0005_briefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE claims (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            brief_id         text NOT NULL,
            symbol           text NOT NULL,
            claim_type       text NOT NULL
                             CHECK (claim_type IN ('catalyst_pending', 'relative_strength',
                                                   'supply_overhang', 'breadth')),
            direction        text NOT NULL CHECK (direction IN ('up', 'down', 'neutral')),
            horizon_sessions integer NOT NULL CHECK (horizon_sessions >= 1),
            session_date     date NOT NULL,
            resolved_at      timestamptz,
            resolved_session date,
            outcome          text CHECK (outcome IN ('correct', 'wrong', 'unresolved')),
            created_at       timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, symbol, claim_type, session_date)
        );
        CREATE INDEX claims_unresolved_idx ON claims (user_id, session_date)
            WHERE outcome IS NULL;

        ALTER TABLE claims ENABLE ROW LEVEL SECURITY;
        CREATE POLICY claims_tenant ON claims
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS claims;")
