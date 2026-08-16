"""`_resolve_symbols`'s market-aware default universe (C1, final review): a
mixed US+CN book must resolve only its own market's symbols per `--market`
value, mirroring `worker.scheduler.book_symbols`'s market filter and
benchmark union — before this fix, `backfill --market cn` without `--symbols`
fed every US holding (and the US benchmark) to the synthetic CN provider on a
mixed book. Real database (skipped without DATABASE_URL).

Not the shared `db_conn` fixture: `_resolve_symbols` takes an `Engine` and
opens its own connection internally (`with engine.connect() as conn`), so
seeding through `db_conn`'s enclosing (uncommitted) transaction would leave
the seeded rows invisible to the separate connection the function under test
opens itself — the same reasoning `tests/test_scheduler_db.py` documents for
`ingest_premarket_for_session`. This test manages its own setup/teardown
against a real `Engine`, scoped to ZZ-prefixed synthetic symbols and a
dedicated throwaway user so it can never collide with real data or another
session's concurrent run."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker.cli import _resolve_symbols
from worker.constants import BENCHMARK_SYMBOL
from worker_cn.constants import CN_BENCHMARK

_US_SECTOR = "ZZ-CLI-US-SECT"
_CN_SECTOR = "ZZ-CLI-CN-SECT"
_US_SYM = "ZZCLIUS"
_CN_SYM = "ZZCLICN.SS"


@pytest.fixture
def engine() -> Iterator[Engine]:
    from worker.config import DATABASE_URL

    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping DB integration test")

    from worker.db import get_engine

    yield get_engine()


def test_mixed_book_resolves_only_its_own_market_per_market_value(engine: Engine) -> None:
    user_id = str(uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email) "
                    "VALUES (:u, 'test-cli-resolve-symbols@example.invalid')"
                ),
                {"u": user_id},
            )
            us_sector = conn.execute(
                text(
                    "INSERT INTO sectors (user_id, name, market) "
                    "VALUES (:u, :n, 'US') RETURNING id"
                ),
                {"u": user_id, "n": _US_SECTOR},
            ).scalar_one()
            cn_sector = conn.execute(
                text(
                    "INSERT INTO sectors (user_id, name, market) "
                    "VALUES (:u, :n, 'CN') RETURNING id"
                ),
                {"u": user_id, "n": _CN_SECTOR},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym)"),
                {"u": user_id, "s": us_sector, "sym": _US_SYM},
            )
            conn.execute(
                text("INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym)"),
                {"u": user_id, "s": cn_sector, "sym": _CN_SYM},
            )

        us_symbols = _resolve_symbols(None, engine, market="US")
        assert _US_SYM in us_symbols
        assert _CN_SYM not in us_symbols
        assert BENCHMARK_SYMBOL in us_symbols
        assert CN_BENCHMARK not in us_symbols

        cn_symbols = _resolve_symbols(None, engine, market="CN")
        assert _CN_SYM in cn_symbols
        assert _US_SYM not in cn_symbols
        assert CN_BENCHMARK in cn_symbols
        assert BENCHMARK_SYMBOL not in cn_symbols
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
