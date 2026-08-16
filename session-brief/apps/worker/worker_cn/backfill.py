"""CN-side ``backfill`` logic (CN-M1). Kept out of ``worker.cli`` so CN logic
stays in the CN package (cn/README.md separation rule) — the CLI only routes
here.

Ingest/normalize are source-scoped (``worker/ingest.py``, ``worker/
normalize.py``) so synthetic-cn rows never collide with, or get replayed
alongside, live Tiingo rows.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

from sqlalchemy.engine import Engine

from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars
from worker_cn.config import cn_bars_are_synthetic
from worker_cn.providers import SyntheticCnBarsProvider

SOURCE = "synthetic-cn"
ENDPOINT = "daily/prices"


class BackfillCnResult(NamedTuple):
    written: int
    bars: int


def backfill_cn(engine: Engine, symbols: list[str], start: date, end: date) -> BackfillCnResult:
    """Ingest + normalize CN daily bars for ``symbols`` over ``[start, end]``.

    Requires ``cn_bars_are_synthetic()`` — the live path does not exist yet
    (CN-M3). Raises ``RuntimeError`` if ``CN_BARS_LIVE`` is set."""
    if not cn_bars_are_synthetic():
        raise RuntimeError("CN live bars land in CN-M3; run tiingo-cn-probe first")

    provider = SyntheticCnBarsProvider()
    with engine.begin() as conn:
        written = ingest_daily_bars(
            conn, provider, symbols, start, end, source=SOURCE, endpoint=ENDPOINT
        )
        bars = normalize_bars(conn, symbols, sources=(SOURCE,))
    return BackfillCnResult(written=written, bars=bars)
