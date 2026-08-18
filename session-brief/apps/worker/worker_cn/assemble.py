"""CN brief assemblers (CN-M1 close, CN-M2 open).

The close path mirrors ``worker.assemble.assemble_and_store``'s orchestration
shape — ``compute_and_store`` → the closes read → the tape compute →
``assemble()`` → upsert — but strips every US-only step. In particular this
must NOT call ``resolve_due_claims``, ``emit_claims``, narration, or the
catalysts readers: the claim ledger and narration are user-wide, and running
them at the CN close would consume the US book's due claims (or spend a
narration call) for a session the US brief hasn't seen yet. ``assemble()`` is
called with empty/none claims, resolved claims, flags, decomposition, and
catalysts.

The open path (CN-M2) is forward-looking and carries no P&L at all — the
"overnight" read for Shanghai is the **US session that closed hours earlier**,
already cached in ``bars_daily`` by the nightly US backfill. It reuses
``worker.assemble_open``'s pure section builders (``_overnight_tape``,
``_calendar``, ``_sector_setup``) and dataclasses (``SectorSetup``,
``trailing_return``) rather than duplicating them, same as the close path
reuses the shared ``assemble()``. Like the close path, it never narrates and
never touches claims — those stay user-wide, single-run concerns.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker.assemble import (
    SCHEMA_VERSION,
    _read_closes,
    _store_brief,
    assemble,
    close_brief_should_skip,
)
from worker.assemble_open import (
    _SECTOR_WINDOW,
    SectorSetup,
    _calendar,
    _overnight_tape,
    _sector_setup,
    trailing_return,
)
from worker.assemble_open import _read_closes as _read_trailing_closes
from worker.assemble_open import _read_events as _read_book_events
from worker.assemble_shared import session_label
from worker.compute import compute_and_store
from worker.constants import BENCHMARK_SYMBOL
from worker.events_seed import CalendarEvent
from worker.premarket import TapeQuote
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

    # No `technicals=` (M19): §4's levels are computed from `bars_daily`, and CN
    # history is still partly synthetic (cn/docs/milestones.md, the CN_BARS_LIVE
    # purge). Levels off synthetic bars would be confidently wrong rather than
    # null, which is the failure mode worth avoiding. §4 still renders RVOL and
    # range position; the snapshot turns on when CN bar provenance is real.
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


# --- Open brief (CN-M2) -----------------------------------------------------


def _open_subject(session_date: date) -> str:
    """No figure — same reasoning as the US open brief's ``_subject``: there is
    no P&L to report, and the "CN Open" label is what tells the two books'
    subjects apart in an inbox."""
    return f"CN Open · {session_label(session_date)} — the day ahead"


def assemble_cn_open(
    *,
    events: list[CalendarEvent],
    sectors: list[SectorSetup],
    tape: list[TapeQuote],
    holdings: dict[str, str],
    user_id: str,
    session_date: date,
    generated_at: datetime,
    missing: list[str] | None = None,
) -> BriefObject:
    """Build a validated ``open_cn`` ``BriefObject``. Pure — no DB, no clock.

    Three sections only (existing section ids, no book, no premarket, no
    exposure_check, no claims): §2 overnight tape is the **US** session's
    day-over-day closes (SPY + the US book's sector benchmarks), §4 calendar
    is seeded events already scoped to the CN book, §5 sector setup is the CN
    sectors' own trailing return vs 510300.SS (the CSI 300 ETF proxy —
    CN_BENCHMARK, CN-Q5). Always sends — no skip gate, matching the US open
    brief (docs/05)."""
    stale = [STALE_CN_BARS_SYNTHETIC] if cn_bars_are_synthetic() else []
    payload = {
        "schema_version": SCHEMA_VERSION,
        "brief_id": f"{user_id}-{session_date.isoformat()}-open_cn",
        "user_id": user_id,
        "session_date": session_date.isoformat(),
        "kind": "open_cn",
        "generated_at": generated_at.isoformat(),
        "subject": _open_subject(session_date),
        "currency": "CNY",
        "one_thing": None,
        "sections": [
            _overnight_tape(tape),
            _calendar(events, holdings, session_date),
            _sector_setup(sectors),
        ],
        "flags": [],
        "claims": [],
        "resolved_claims": [],
        "suppressed": [],
        "data_quality": {"missing": missing or [], "stale": stale},
    }
    # model_validate enforces the contract (extra='forbid', required keys, types).
    return BriefObject.model_validate(payload)


_READ_CN_SECTORS = text("""
    SELECT id, name, benchmark_symbol FROM sectors
    WHERE user_id = :user_id AND market = 'CN' ORDER BY sort_order, name
""")

_READ_CN_HOLDINGS = text("""
    SELECT h.symbol, h.status FROM holdings h
    JOIN sectors s ON s.id = h.sector_id AND s.user_id = h.user_id
    WHERE h.user_id = :user_id AND s.market = 'CN'
""")

# The US session's last two closes as of (and including) ``session_date`` —
# CN's morning bell always trails the prior US close, never overlaps it, so a
# plain cutoff finds the right pair without a separate "prior US session"
# calendar lookup.
_READ_TWO_CLOSES = text("""
    SELECT c FROM (
        SELECT session_date, c FROM bars_daily
        WHERE symbol = :symbol AND session_date <= :session_date
        ORDER BY session_date DESC LIMIT 2
    ) recent ORDER BY session_date
""")


def _read_cn_sectors(
    conn: Connection, user_id: str, prior_session: date
) -> list[SectorSetup]:
    """§5's rows: the CN sectors' own benchmark, 5-session trailing return, and
    the relative return vs 510300.SS (``CN_BENCHMARK`` — the CSI 300 ETF proxy,
    CN-Q5) — the CN-book mirror of ``worker.assemble_open._read_sectors``,
    scoped to ``market = 'CN'`` and measured against the CN benchmark rather
    than SPY."""
    cn_bench_5d = trailing_return(
        _read_trailing_closes(conn, CN_BENCHMARK, prior_session), _SECTOR_WINDOW
    )
    rows = conn.execute(_READ_CN_SECTORS, {"user_id": user_id}).mappings()
    out: list[SectorSetup] = []
    for row in rows:
        benchmark = row["benchmark_symbol"]
        ret_5d = (
            trailing_return(_read_trailing_closes(conn, benchmark, prior_session), _SECTOR_WINDOW)
            if benchmark
            else None
        )
        out.append(
            SectorSetup(
                sector_id=str(row["id"]),
                name=str(row["name"]),
                benchmark_symbol=benchmark,
                ret_5d=ret_5d,
                vs_spy_5d=(
                    ret_5d - cn_bench_5d
                    if ret_5d is not None and cn_bench_5d is not None
                    else None
                ),
            )
        )
    return out


def _read_cn_holdings(conn: Connection, user_id: str) -> dict[str, str]:
    """§4's tagging input, scoped to the CN book only — the US book's holdings
    must never decide whether a CN calendar row reads as ``holding`` or
    ``watchlist``."""
    return {
        str(row["symbol"]): str(row["status"])
        for row in conn.execute(_READ_CN_HOLDINGS, {"user_id": user_id}).mappings()
    }


def _read_us_overnight_tape(
    conn: Connection, user_id: str, session_date: date
) -> list[TapeQuote]:
    """§2's rows: SPY plus every benchmark set on the **US** book's sectors —
    already ingested nightly by the US backfill, so this reads real Tiingo
    ``bars_daily`` levels, not a synthetic feed. A symbol with fewer than two
    cached closes is omitted, never invented."""
    from worker.scheduler import sector_benchmarks

    symbols = [BENCHMARK_SYMBOL, *sector_benchmarks(conn, user_id, market="US")]
    out: list[TapeQuote] = []
    for symbol in symbols:
        closes = [
            Decimal(str(r[0]))
            for r in conn.execute(
                _READ_TWO_CLOSES, {"symbol": symbol, "session_date": session_date}
            ).all()
        ]
        if len(closes) == 2:
            out.append(
                TapeQuote(symbol=symbol, label=symbol, last=closes[-1], prev_close=closes[0])
            )
    return out


def assemble_cn_open_and_store(
    conn: Connection,
    user_id: str,
    session_date: date,
    *,
    prior_session: date,
    generated_at: datetime,
) -> BriefObject:
    """Read the cached inputs, assemble an ``open_cn`` brief, and upsert it.

    Always returns an object — no skip gate, the open brief always sends
    (docs/05). Idempotent on ``(user_id, session_date, kind)``. Never touches
    the claim ledger or narration — both are user-wide, single-run concerns
    the US open brief already owns for this session (same reasoning as the
    CN close path's docstring)."""
    from worker.scheduler import book_symbols

    cn_book = book_symbols(conn, user_id, market=CN_MARKET, benchmark=CN_BENCHMARK)
    events = _read_book_events(conn, session_date, cn_book)
    sectors = _read_cn_sectors(conn, user_id, prior_session)
    tape = _read_us_overnight_tape(conn, user_id, session_date)
    holdings = _read_cn_holdings(conn, user_id)

    obj = assemble_cn_open(
        events=events,
        sectors=sectors,
        tape=tape,
        holdings=holdings,
        user_id=user_id,
        session_date=session_date,
        generated_at=generated_at,
    )
    _store_brief(conn, obj)
    return obj
