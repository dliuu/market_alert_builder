"""open brief §4: widen `events` to hold a real calendar (M14)

M7 created `events` for one job — the lockup supply-event flag — so it holds
only what that needed. The open brief's "On the clock today" (docs/05 §4) needs
three things it can't express:

- **`ex_div`** is not in the CHECK. §4 lists macro releases, earnings, lockups
  *and* ex-div.
- **`symbol` is NOT NULL**, but a macro release (CPI, FOMC) names no security.
- **No label.** §4 renders "CPI (Jul)", not "macro". Storing it in `payload`
  would work, but every calendar row has one — it belongs in a column.

Also adds a uniqueness key so seeding is re-runnable (the M7 fundamentals
pattern). `NULLS NOT DISTINCT` so two macro rows on the same date can't
duplicate — the same construction the `flags` table uses for its nullable
symbol/sector key.

Revision ID: 0010_open_events
Revises: 0009_attribution
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_open_events"
down_revision: str | None = "0009_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE events DROP CONSTRAINT events_event_type_check;
        ALTER TABLE events ADD CONSTRAINT events_event_type_check
            CHECK (event_type IN ('earnings', 'lockup', 'ex_div', 'macro'));

        ALTER TABLE events ALTER COLUMN symbol DROP NOT NULL;
        ALTER TABLE events ADD COLUMN label text;

        ALTER TABLE events ADD CONSTRAINT events_calendar_key
            UNIQUE NULLS NOT DISTINCT (symbol, event_type, occurs_at);
    """)


def downgrade() -> None:
    # Rows the old shape can't hold have to go before the constraints come back.
    op.execute("""
        ALTER TABLE events DROP CONSTRAINT events_calendar_key;

        DELETE FROM events WHERE symbol IS NULL OR event_type = 'ex_div';

        ALTER TABLE events DROP COLUMN label;
        ALTER TABLE events ALTER COLUMN symbol SET NOT NULL;

        ALTER TABLE events DROP CONSTRAINT events_event_type_check;
        ALTER TABLE events ADD CONSTRAINT events_event_type_check
            CHECK (event_type IN ('earnings', 'lockup', 'macro'));
    """)
