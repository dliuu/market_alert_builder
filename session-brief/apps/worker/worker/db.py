"""Database engine. The worker owns the schema; Alembic is the only migration path."""

from sqlalchemy import Engine, create_engine

from worker.config import DATABASE_URL


def get_engine() -> Engine:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in the "
            "Supabase connection string (see docs/09-supabase-setup.md)."
        )
    return create_engine(DATABASE_URL)
