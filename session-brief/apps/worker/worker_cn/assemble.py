"""CN close brief assembler (CN-M1): compute → tape → the pure shared
``assemble()`` → stored brief.

Mirrors ``worker.assemble.assemble_and_store``'s orchestration shape —
``compute_and_store`` → the closes read → the tape compute → ``assemble()`` →
upsert — but strips every US-only step. In particular this must NOT call
``resolve_due_claims``, ``emit_claims``, narration, or the catalysts readers:
the claim ledger and narration are user-wide, and running them at the CN
close would consume the US book's due claims (or spend a narration call) for
a session the US brief hasn't seen yet. ``assemble()`` is called with empty/
none claims, resolved claims, flags, decomposition, and catalysts.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker.assemble import _read_closes, _store_brief, assemble, close_brief_should_skip
from worker.compute import compute_and_store
from worker.tape import compute_and_store_tape
from worker_cn.config import cn_bars_are_synthetic
from worker_cn.constants import CN_BENCHMARK, CN_MARKET

# `data_quality.stale` entry marking the brief as built on invented levels
# rather than a live CN print (CN-M3 flips this off once the live feed lands).
STALE_CN_BARS_SYNTHETIC = "cn_bars.synthetic"


def assemble_cn_close_and_store(
    conn: Connection, user_id: str, session_date: date
) -> BriefObject | None:
    """Compute the CN book, assemble a ``close_cn`` brief, and upsert it.

    Returns ``None`` (writing nothing) when the session was quiet — the same
    close-brief skip gate the US path shares (``close_brief_should_skip``).
    Raises ``ValueError`` if the user has no CN holdings. Idempotent on
    ``(user_id, session_date, kind)``: re-running replaces the row rather than
    duplicating it.
    """
    result = compute_and_store(
        conn, user_id, session_date, market=CN_MARKET, benchmark=CN_BENCHMARK
    )
    if not result.positions:
        raise ValueError(f"no CN holdings for user {user_id}")

    symbols = [p.symbol for p in result.positions]
    closes = _read_closes(conn, symbols, session_date)
    tape = compute_and_store_tape(conn, user_id, symbols, session_date)

    stale = [STALE_CN_BARS_SYNTHETIC] if cn_bars_are_synthetic() else []

    obj = assemble(
        result,
        closes,
        tape,
        user_id=user_id,
        session_date=session_date,
        kind="close_cn",
        generated_at=datetime.now(UTC),
        currency="CNY",
        stale=stale,
    )
    if close_brief_should_skip(obj):
        return None

    _store_brief(conn, obj)
    return obj
