"""Connection-pool resilience (the 2026-08-28 close-brief failure).

The worker is one long-lived process whose jobs are hours apart: the open brief
sends at 12:15 UTC and the close does not touch the database again until 20:45.
A pooled connection idles across that gap, and anything on the client↔pooler leg
that closes it (a Supavisor restart, an idle-flow reaper) leaves the pool holding
a dead socket. Handed out unvalidated, the next statement reads EOF instead of a
response:

    OperationalError('(psycopg.OperationalError) consuming input failed:
                      SSL error: unexpected eof while reading')

which is what cost 2026-08-28's close brief. `pool_pre_ping` is the fix; these
tests pin it.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import text

DUMMY_URL = "postgresql+psycopg://u:p@localhost:5432/db"


def test_engine_validates_a_pooled_connection_before_handing_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this the pool trusts a socket it has not touched in eight hours."""
    import worker.db as db

    monkeypatch.setattr(db, "DATABASE_URL", DUMMY_URL)
    engine = db.get_engine()  # create_engine is lazy; nothing connects here

    assert engine.pool._pre_ping is True


def test_engine_recycles_connections_rather_than_holding_them_for_the_process_life(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default is -1 (never). The worker ran 11 days on one engine."""
    import worker.db as db

    monkeypatch.setattr(db, "DATABASE_URL", DUMMY_URL)
    engine = db.get_engine()

    assert 0 < engine.pool._recycle <= 1800


def test_a_pooled_connection_killed_by_the_peer_is_replaced_not_raised() -> None:
    """The real reproduction: tear the socket down underneath the pool exactly as
    an idle-flow reaper does — no FATAL packet, just EOF — then use the pool."""
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    engine = get_engine()
    try:
        conn = engine.connect()
        conn.execute(text("SELECT 1"))
        fd = conn.connection.dbapi_connection.pgconn.socket  # type: ignore[union-attr]
        conn.close()  # back into the pool, believed healthy

        sock = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()

        with engine.connect() as fresh:
            assert fresh.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
