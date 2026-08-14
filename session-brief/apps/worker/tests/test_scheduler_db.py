"""`ingest_premarket_for_session` against a real database (skipped without
DATABASE_URL) — the one integration test for the function that actually wires
provider -> `quotes` in production (I4, M15 review). It is the regression
guard for C2 (the tape's bootstrap seed, `TAPE_SEED_LEVELS`) and I3 (the held/
tape upsert split) together: before those fixes, this test's §2 assertion
failed (empty tape, forever) and a held name that also appeared on the tape
would have lost its §3 row.

Not the shared `db_conn` fixture: `ingest_premarket_for_session` takes an
`Engine` and opens and commits its own connections/transactions internally, so
wrapping the *test* in one enclosing transaction wouldn't roll back what the
function under test itself commits. This test manages its own setup/teardown
against a real `Engine`, scoped to synthetic symbols and a far-future session
so it can never collide with real data.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker.assemble_open import read_open_inputs
from worker.constants import TAPE_SYMBOLS
from worker.scheduler import ingest_premarket_for_session

_SESSION = date(2098, 6, 10)
_PRIOR = date(2098, 6, 9)
_HELD = ["ZI4A", "ZI4B", "ZI4C", "ZI4D", "ZI4E"]
_TAPE_SYMBOLS = [s for s, _label, _feed in TAPE_SYMBOLS]


@pytest.fixture
def engine() -> Iterator[Engine]:
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    yield get_engine()


def test_the_capture_populates_both_the_tape_and_the_names(engine: Engine) -> None:
    """DoD: after one capture, `read_open_inputs` returns non-empty §2 *and*
    §3 — the empirical check the whole-branch review used to find C2, run in
    reverse."""
    user_id = str(uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": user_id, "e": f"{user_id}@example.invalid"},
            )
            sector_id = conn.execute(
                text("INSERT INTO sectors (user_id, name) VALUES (:u, 'I4 Test') "
                     "RETURNING id"),
                {"u": user_id},
            ).scalar()
            for symbol in _HELD:
                conn.execute(
                    text("INSERT INTO holdings (user_id, sector_id, symbol, status) "
                         "VALUES (:u, :sec, :s, 'owned')"),
                    {"u": user_id, "sec": sector_id, "s": symbol},
                )
                conn.execute(
                    text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, "
                         "v, adj_c) VALUES (:s, :d, 40, 40, 40, 40, 1000, 40)"),
                    {"s": symbol, "d": _PRIOR},
                )

        written = ingest_premarket_for_session(
            engine, session_date=_SESSION, prior_session=_PRIOR, user_id=user_id
        )
        # 5 held names + the 6 fixed tape symbols (no sector benchmark is set,
        # so no foreign proxy joins the tape universe for this test book).
        assert written == len(_HELD) + len(_TAPE_SYMBOLS)

        with engine.connect() as conn:
            quotes_count = conn.execute(
                text("SELECT count(*) FROM quotes WHERE session_date = :d"),
                {"d": _SESSION},
            ).scalar()
            assert quotes_count == len(_HELD) + len(_TAPE_SYMBOLS)

            events, sectors, holdings, tape, premarket = read_open_inputs(
                conn, user_id, _SESSION, _PRIOR
            )
        # C2's regression: nothing ingests bars for the tape symbols, and
        # without `TAPE_SEED_LEVELS` this list is empty on every run, forever.
        assert tape, "§2 (overnight tape) came back empty — the C2 regression"
        # I3's regression companion: every held name captured a row (none of
        # them also appear on this test's tape, so I3's overlap isn't
        # exercised here — see test_premarket_db.py for that case directly).
        assert len(premarket) == len(_HELD), "§3 (pre-market names) lost a row"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM quotes WHERE session_date = :d "
                     "AND symbol = ANY(:syms)"),
                {"d": _SESSION, "syms": _HELD + _TAPE_SYMBOLS},
            )
            conn.execute(
                text("DELETE FROM bars_daily WHERE session_date = :d "
                     "AND symbol = ANY(:syms)"),
                {"d": _PRIOR, "syms": _HELD},
            )
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
