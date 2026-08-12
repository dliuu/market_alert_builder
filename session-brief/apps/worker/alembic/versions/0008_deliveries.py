"""deliveries: one row per (brief, recipient) send attempt (M9)

Stage ⑦ deliver writes a ``pending`` row before calling Resend and flips it to
``sent`` with the provider message id on success (docs/06). ``UNIQUE (brief_id,
recipient)`` is the idempotency guard (invariant #6): a crashed worker's retry
re-selects the existing row rather than mailing the same brief twice.

RLS keys on user_id like the other tenant tables (D7/D10); the worker writes as
table owner.

Revision ID: 0008_deliveries
Revises: 0007_flags
Create Date: 2026-08-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_deliveries"
down_revision: str | None = "0007_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE deliveries (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            brief_id        uuid NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
            recipient       text NOT NULL,
            status          text NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'sent', 'failed',
                                                  'bounced', 'complained')),
            provider_msg_id text,
            error           text,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now(),
            UNIQUE (brief_id, recipient)
        );
        CREATE INDEX deliveries_user_idx ON deliveries (user_id);

        ALTER TABLE deliveries ENABLE ROW LEVEL SECURITY;
        CREATE POLICY deliveries_tenant ON deliveries
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deliveries;")
