"""briefs: the assembled BriefObject, one row per (user, session, kind) (M4)

Stage ④ assemble writes a versioned BriefObject into ``body`` (jsonb); the web
archive and (later) the email render over it (docs/04). ``schema_version`` is
stored alongside so an old row re-renders under the renderer it was written for.

UNIQUE (user_id, session_date, kind) makes re-assembly an upsert, not a
duplicate — the same idempotency shape the delivery path relies on. RLS keys on
user_id like the book tables (D7/D10); the worker writes as table owner.

Revision ID: 0005_briefs
Revises: 0004_metrics
Create Date: 2026-08-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_briefs"
down_revision: str | None = "0004_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE briefs (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_date   date NOT NULL,
            kind           text NOT NULL CHECK (kind IN ('open', 'close')),
            schema_version integer NOT NULL,
            body           jsonb NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, session_date, kind)
        );
        CREATE INDEX briefs_user_date_idx ON briefs (user_id, session_date);

        ALTER TABLE briefs ENABLE ROW LEVEL SECURITY;
        CREATE POLICY briefs_tenant ON briefs
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS briefs;")
