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
from worker_cn.providers import default_cn_bars_provider

SYNTHETIC_SOURCE = "synthetic-cn"
LIVE_SOURCE = "tiingo"  # matches worker/ingest.py's US default namespace
ENDPOINT = "daily/prices"


class BackfillCnResult(NamedTuple):
    written: int
    bars: int


def backfill_cn(engine: Engine, symbols: list[str], start: date, end: date) -> BackfillCnResult:
    """Ingest + normalize CN daily bars for ``symbols`` over ``[start, end]``.

    Routes through ``default_cn_bars_provider()`` (CN-M3): synthetic bars under
    ``source="synthetic-cn"`` while ``cn_bars_are_synthetic()``, live Tiingo
    bars under ``source="tiingo"`` once ``CN_BARS_LIVE`` is set — the real
    vendor's namespace, so synthetic and live CN rows never collide."""
    source = SYNTHETIC_SOURCE if cn_bars_are_synthetic() else LIVE_SOURCE
    provider = default_cn_bars_provider()
    with engine.begin() as conn:
        written = ingest_daily_bars(
            conn, provider, symbols, start, end, source=source, endpoint=ENDPOINT
        )
        bars = normalize_bars(conn, symbols, sources=(source,))
    return BackfillCnResult(written=written, bars=bars)
