"""flags + fundamentals + events: position risk and the correlation flag (M7)

Stage ④ thresholds these (docs/05). ``fundamentals`` and ``events`` carry **no
user_id** — they're shared market data keyed by symbol, like bars_daily, so
ingest cost scales with the symbol universe, not the user count (docs/03). M7
seeds them synthetically; real earnings/fundamentals ingest is deferred (docs/02
marks them TBD source gaps).

``flags`` carries user_id (concentration/correlation/runway are book-specific)
and is what enforces the correlation flag's once-a-week email cap: ``last_seen``
is the last session the flag was surfaced ("when did I last warn myself?"),
``first_seen`` the first. UNIQUE NULLS NOT DISTINCT lets the (nullable) symbol /
sector_id participate in the upsert key (Postgres 16, per docs/02). RLS keys on
user_id like the book tables (D7/D10); the worker writes as table owner.

Revision ID: 0007_flags
Revises: 0006_claims
Create Date: 2026-08-11

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_flags"
down_revision: str | None = "0006_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fundamentals (
            symbol               text NOT NULL,
            as_of                date NOT NULL,
            cash_cents           bigint,
            quarterly_burn_cents bigint,
            shares_out           bigint,
            next_earnings_date   date,
            PRIMARY KEY (symbol, as_of)
        );

        CREATE TABLE events (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            symbol     text NOT NULL,
            event_type text NOT NULL CHECK (event_type IN ('earnings', 'lockup', 'macro')),
            occurs_at  date NOT NULL,
            payload    jsonb NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX events_symbol_occurs_idx ON events (symbol, occurs_at);

        CREATE TABLE flags (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            flag_type  text NOT NULL
                       CHECK (flag_type IN ('concentration', 'correlation', 'runway',
                                            'dilution', 'earnings_soon', 'supply_event',
                                            'short_interest')),
            symbol     text,
            sector_id  uuid,
            first_seen date NOT NULL,
            last_seen  date NOT NULL,
            severity   text NOT NULL CHECK (severity IN ('info', 'warn')),
            payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE NULLS NOT DISTINCT (user_id, flag_type, symbol, sector_id)
        );
        CREATE INDEX flags_user_idx ON flags (user_id);
    """)

    # fundamentals/events are shared reference data (like bars_daily): RLS on
    # with a read-only public policy; the worker writes as table owner (D10).
    op.execute("""
        ALTER TABLE fundamentals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE events       ENABLE ROW LEVEL SECURITY;
        ALTER TABLE flags        ENABLE ROW LEVEL SECURITY;

        CREATE POLICY fundamentals_read ON fundamentals FOR SELECT USING (true);
        CREATE POLICY events_read       ON events       FOR SELECT USING (true);
        CREATE POLICY flags_tenant ON flags
            USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS flags;
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS fundamentals;
    """)
