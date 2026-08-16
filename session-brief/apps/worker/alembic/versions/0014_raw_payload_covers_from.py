"""raw_payloads: key a payload by the window it covers, not just where it ends

Ingest keys a payload by ``(source, endpoint, symbol, as_of)``, where ``as_of``
is the newest session the response contains. That makes a *wider* re-fetch
ending on the same date indistinguishable from a duplicate: a 260-day backfill
and a 10-day poll that both end 2026-08-14 produce the same tuple, so
``ON CONFLICT DO NOTHING`` discards the deeper history and a symbol's window can
never be extended. Observed 2026-08-14: a 260-day backfill over 10 symbols wrote
6 rows and silently dropped the 4 symbols that already had a payload ending that
day — leaving SPY at 65 sessions and capping the attribution fit at 64
observations instead of FIT_WINDOW's 120.

The dedup itself is load-bearing and must survive: the close job's bar poll
re-fetches the same window every 90s (D20), and without it every poll writes a
row. So the fix is to widen the key to the payload's actual identity — the
window it covers, both ends:

    (source, endpoint, symbol, covers_from, as_of)

``covers_from`` is the *earliest* session in the response, the mirror of the
``as_of`` the same code already computes. A re-fetch of the same window still
collides and no-ops; a wider window has an earlier ``covers_from`` and inserts.

Why a plain backfilled column and not a content hash. A hash keyed on the body
is the more general answer (it would also catch a vendor *revising* a bar inside
an unchanged window), but it cannot be expressed here: ``body::text`` is STABLE,
not IMMUTABLE, because jsonb's output function depends on run-time settings — so
Postgres rejects it in both a generated column and an expression index. Hashing
in Python instead would leave the pre-existing rows unhashable by the same
function, and the first re-fetch of each would insert a spurious duplicate.
``covers_from`` has neither problem: it is derivable from the stored jsonb
exactly as Python derives it, so the backfill below and all future writes agree
by construction.

**Known limitation, unchanged by this migration:** a vendor revision *within* an
identical window still no-ops, exactly as it does today. `normalize` replays
latest-fetch-wins per (symbol, session_date), so a revision that does land is
applied — but ingest will not store one unless the window's bounds differ. That
is the same behaviour as before this change, not a regression; closing it needs
the content hash and therefore a different mechanism.

`normalize` already replays every payload latest-fetch-wins per
``(symbol, session_date)``, so a narrow and a wide payload coexisting is the
design rather than a conflict. Invariant 5 holds: nothing is mutated, only
added.

Revision ID: 0014_raw_payload_covers_from
Revises: 0013_claims_premarket_gap
Create Date: 2026-08-15

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_raw_payload_covers_from"
down_revision: str | None = "0013_claims_premarket_gap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The backfill mirrors worker/ingest.py's `_session_date`: the record's
    # "date" field truncated to its first 10 chars (the vendor sends a full
    # timestamp). Derived from the stored payload, so every existing row gets
    # the value Python would have written. Snapshot payloads (fdn latest-prices
    # / index-quotes / futures-prices / stock-quotes) carry no per-record date,
    # so they fall back to `as_of` — a snapshot covers its own capture date,
    # which is also what the fdn writers stamp going forward.
    op.execute("""
        ALTER TABLE raw_payloads ADD COLUMN covers_from date;

        UPDATE raw_payloads SET covers_from = COALESCE(
            (SELECT min(left(elem->>'date', 10)::date)
             FROM jsonb_array_elements(body) AS elem),
            as_of
        );

        ALTER TABLE raw_payloads ALTER COLUMN covers_from SET NOT NULL;

        ALTER TABLE raw_payloads
            DROP CONSTRAINT raw_payloads_source_endpoint_symbol_as_of_key;

        ALTER TABLE raw_payloads
            ADD CONSTRAINT raw_payloads_window_key
            UNIQUE (source, endpoint, symbol, covers_from, as_of);
    """)


def downgrade() -> None:
    # Narrowing the key can only succeed while no two rows share
    # (source, endpoint, symbol, as_of) — i.e. before any deeper window has been
    # ingested under the new key. Inherent to reverting a widened unique key,
    # not an oversight: drop the redundant rows first if you need to go back.
    op.execute("""
        ALTER TABLE raw_payloads DROP CONSTRAINT raw_payloads_window_key;

        ALTER TABLE raw_payloads
            ADD CONSTRAINT raw_payloads_source_endpoint_symbol_as_of_key
            UNIQUE (source, endpoint, symbol, as_of);

        ALTER TABLE raw_payloads DROP COLUMN covers_from;
    """)
