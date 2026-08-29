"""Database engine. The worker owns the schema; Alembic is the only migration path."""

from sqlalchemy import Engine, create_engine

from worker.config import DATABASE_URL

# The worker is a single long-lived process (Fly.io, no cold starts) whose jobs
# are hours apart — the open brief sends at 12:15 UTC and the close does not
# touch the database again until 20:45. A pooled connection idles across that
# gap, and anything on the client↔pooler leg that closes it (a Supavisor restart,
# an idle-flow reaper) leaves the pool holding a dead socket. SQLAlchemy's
# defaults never validate one, so the next statement reads EOF instead of a
# response and the job dies — which is how 2026-08-28's close brief was lost.
#
# `pool_pre_ping` spends one round-trip per checkout to validate the connection
# and transparently replaces a dead one. `pool_recycle` caps connection age below
# the idle windows these reapers use, so most of them are retired before they are
# ever handed out.
POOL_PRE_PING = True
POOL_RECYCLE_S = 1800


def get_engine() -> Engine:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in the "
            "Supabase connection string (see docs/09-supabase-setup.md)."
        )
    return create_engine(
        DATABASE_URL,
        pool_pre_ping=POOL_PRE_PING,
        pool_recycle=POOL_RECYCLE_S,
    )
