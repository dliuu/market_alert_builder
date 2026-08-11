"""book tables: users, sectors, holdings, lots

The book is the app's data-entry surface (M1). Every table carries user_id
(D7), RLS is enabled from the first migration with auth.uid()-keyed policies
(D10), and a single dev user is seeded as the tenancy anchor for single-user
development — replaced by Supabase Auth when user #2 exists.

Money is integer cents (cost_basis_cents is per-share cost). Positions are
modelled as lots, never an average cost on holdings.

Revision ID: 0002_book
Revises: 0001_initial
Create Date: 2026-08-10

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_book"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed UUID for the single-user dev book. The web app references this until
# Supabase Auth lands and auth.uid() drives tenancy.
DEV_USER_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email      text NOT NULL UNIQUE,
            tz         text NOT NULL DEFAULT 'America/New_York',
            created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE sectors (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name             text NOT NULL,
            benchmark_symbol text,
            sort_order       integer NOT NULL DEFAULT 0,
            created_at       timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, name)
        );
        CREATE INDEX sectors_user_id_idx ON sectors (user_id);

        CREATE TABLE holdings (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sector_id  uuid NOT NULL REFERENCES sectors(id) ON DELETE CASCADE,
            symbol     text NOT NULL,
            status     text NOT NULL DEFAULT 'owned'
                       CHECK (status IN ('owned', 'watching')),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, symbol)
        );
        CREATE INDEX holdings_user_id_idx ON holdings (user_id);
        CREATE INDEX holdings_sector_id_idx ON holdings (sector_id);

        CREATE TABLE lots (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            holding_id      uuid NOT NULL REFERENCES holdings(id) ON DELETE CASCADE,
            shares          numeric(20, 6) NOT NULL,
            cost_basis_cents bigint NOT NULL,
            opened_on       date NOT NULL,
            closed_on       date,
            created_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX lots_user_id_idx ON lots (user_id);
        CREATE INDEX lots_holding_id_idx ON lots (holding_id);
    """)

    # RLS from the first migration (D7/D10). Policies key on auth.uid(); the
    # table owner (the migration/worker role) bypasses them, so single-user dev
    # over the direct connection still works. When Supabase Auth lands, the web
    # app connects as the authenticated role and these become the tenancy guard.
    op.execute("""
        ALTER TABLE users    ENABLE ROW LEVEL SECURITY;
        ALTER TABLE sectors  ENABLE ROW LEVEL SECURITY;
        ALTER TABLE holdings ENABLE ROW LEVEL SECURITY;
        ALTER TABLE lots     ENABLE ROW LEVEL SECURITY;

        CREATE POLICY users_self ON users
            USING (id = auth.uid()) WITH CHECK (id = auth.uid());
        CREATE POLICY sectors_tenant ON sectors
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
        CREATE POLICY holdings_tenant ON holdings
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
        CREATE POLICY lots_tenant ON lots
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)

    # Seed the single-user dev tenant.
    op.execute(f"""
        INSERT INTO users (id, email)
        VALUES ('{DEV_USER_ID}', 'dev@session-brief.local')
        ON CONFLICT (id) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS lots;
        DROP TABLE IF EXISTS holdings;
        DROP TABLE IF EXISTS sectors;
        DROP TABLE IF EXISTS users;
    """)
