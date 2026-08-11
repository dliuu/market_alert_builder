"""Shared fixtures. The DB fixture runs each test inside a transaction that is
rolled back, so integration tests leave no trace in Supabase. Tests that need
it are skipped when DATABASE_URL is unset (e.g. in CI)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Connection


@pytest.fixture
def db_conn() -> Iterator[Connection]:
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    engine = get_engine()
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()
