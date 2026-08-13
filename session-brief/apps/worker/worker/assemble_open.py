"""Stage ④ for the open brief: cached reads → validated BriefObject (M14).

The open brief is the forward-looking one (docs/01: 08:15 ET, "what changed
overnight, what to be ready for"). It carries **no performance and no P&L**
(docs/05), which is why this is a separate assembler rather than a branch inside
``assemble.py``: it never touches ``ComputeResult``, so the M3-certified P&L path
and its invariant-3 machinery stay exactly as certified.

Three consequences shape the module:

- **No ``book``.** The object omits the key entirely rather than zeroing it —
  "no P&L" is the truth, and a zeroed book is a lie that renders.
- **No skip gate.** The close brief is skipped on a quiet session; the open brief
  always sends, because "nothing happened overnight" is information you need
  before the bell (docs/05).
- **No claims.** M14 emits none (nothing directional is judgeable from cached
  setup — the horizon-0 morning claim needs the pre-market signal, M15) and
  *resolves* none. Resolving at 08:15 would work and that is precisely the trap:
  it would consume the due claims before the close brief's §7 could report them.

Everything here is pure over its inputs with ``generated_at`` injected, so a
frozen fixture snapshots the whole object (the M4 discipline).

§2 (overnight tape) and §3 (pre-market names) need feeds that don't exist yet and
are emitted as suppressed sections carrying a note — the M5 precedent, where a
shorter brief says what it left out rather than silently shrinking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker.assemble import SCHEMA_VERSION
from worker.assemble_shared import session_label
from worker.events_seed import CalendarEvent
from worker.flags import FlagCandidate, candidate_dict

# §5's trailing window (docs/05: "benchmark 5d, vs SPY 5d").
_SECTOR_WINDOW = 5

# §4 looks this far ahead. "On the clock today" leads with today, but a brief
# that only ever showed today would bury a Monday lockup on Friday afternoon.
_CALENDAR_WINDOW_DAYS = 7

_OMITTED_TAPE = "Overnight tape lands with the futures and macro feed."
_OMITTED_PREMARKET = "Pre-market moves land with the delayed-quote feed."
_CLEAR_CALENDAR = "Nothing on the calendar."
_NO_FLAGS = "Nothing over threshold."


@dataclass(frozen=True)
class SectorSetup:
    """One §5 row. ``ret_5d``/``vs_spy_5d`` are None when the sector has no
    benchmark set in the book, or the benchmark has too little history."""

    sector_id: str
    name: str
    benchmark_symbol: str | None
    ret_5d: Decimal | None
    vs_spy_5d: Decimal | None


def trailing_return(closes: list[Decimal], window: int) -> Decimal | None:
    """Return over the last ``window`` sessions from a chronological close
    series. Needs ``window + 1`` closes; ``None`` when the history is too short
    or the base is zero. Decimal, not float — these are prices."""
    if len(closes) < window + 1:
        return None
    base = closes[-(window + 1)]
    if base == 0:
        return None
    return closes[-1] / base - 1


def assemble_open(
    *,
    events: list[CalendarEvent],
    sectors: list[SectorSetup],
    flags: list[FlagCandidate],
    holdings: dict[str, str],
    user_id: str,
    session_date: date,
    prior_session: date,
    generated_at: datetime,
    missing: list[str] | None = None,
    stale: list[str] | None = None,
) -> BriefObject:
    """Build a validated open ``BriefObject``.

    ``holdings`` maps symbol → status (``owned``/``watching``) and drives §4's
    tag. ``prior_session`` is the session every figure is measured from — at
    08:15 on ``session_date`` the last close is the previous trading day's, which
    is not always yesterday (a Tuesday after a Monday holiday looks back to
    Friday). ``generated_at`` is injected, never ``datetime.now()``.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "brief_id": f"{user_id}-{session_date.isoformat()}-open",
        "user_id": user_id,
        "session_date": session_date.isoformat(),
        "kind": "open",
        "generated_at": generated_at.isoformat(),
        "subject": _subject(session_date),
        "one_thing": None,  # narration, stage ⑤
        # `book` is deliberately absent, not null: no performance, no P&L.
        "sections": [
            _omitted("overnight_tape", _OMITTED_TAPE),
            _omitted("premarket", _OMITTED_PREMARKET),
            _calendar(events, holdings, session_date),
            _sector_setup(sectors),
            _exposure_check(flags),
        ],
        "flags": [candidate_dict(f) for f in flags],
        "claims": [],  # M15 lands the horizon-0 morning claim
        "resolved_claims": [],  # resolution stays in the close brief (D16b)
        "suppressed": [],  # per-name suppression is a close-brief mechanism
        "data_quality": {
            "missing": missing or [],
            "stale": stale or [],
        },
    }
    # model_validate enforces the contract (extra='forbid', required keys, types).
    return BriefObject.model_validate(payload)


def _subject(session_date: date) -> str:
    """No figure in the subject — there is no P&L to report, and the open brief
    leads with what's ahead rather than what happened."""
    return f"Open · {session_label(session_date)} — the day ahead"


def _omitted(section_id: str, note: str) -> dict[str, object]:
    """A section M14 can't fill, saying so. Suppressed with a note beats absent:
    the reader learns the brief is incomplete by design, not broken."""
    return {"id": section_id, "tier": "suppressed", "note": note, "rows": []}


def _calendar(
    events: list[CalendarEvent], holdings: dict[str, str], session_date: date
) -> dict[str, object]:
    """§4 On the clock today — macro releases, earnings, lockups, ex-div inside
    the forward window, each tagged with whose calendar it is (docs/05)."""
    horizon = session_date.fromordinal(session_date.toordinal() + _CALENDAR_WINDOW_DAYS)
    upcoming = [e for e in events if session_date <= e.occurs_at <= horizon]
    upcoming.sort(key=lambda e: (e.occurs_at, e.event_type, e.symbol or ""))

    rows = [
        {
            "symbol": e.symbol,
            "label": e.label,
            "event_type": e.event_type,
            "occurs_at": e.occurs_at.isoformat(),
            "tag": _tag(e, holdings),
        }
        for e in upcoming
    ]
    return {
        "id": "calendar",
        "tier": "full" if rows else "suppressed",
        "note": None if rows else _CLEAR_CALENDAR,
        "rows": rows,
    }


def _tag(event: CalendarEvent, holdings: dict[str, str]) -> str:
    """`macro` / `holding` / `watchlist` (docs/05). A symbol that is neither held
    nor watched can still appear — a sector benchmark's ex-div, say — and reads
    as watchlist rather than being dropped."""
    if event.symbol is None:
        return "macro"
    return "holding" if holdings.get(event.symbol) == "owned" else "watchlist"


def _sector_setup(sectors: list[SectorSetup]) -> dict[str, object]:
    """§5 Sector setup — benchmark 5d and vs-SPY 5d per sector. The pre-market
    column is null until M15."""
    rows = [
        {
            "sector_id": s.sector_id,
            "name": s.name,
            "benchmark_symbol": s.benchmark_symbol,
            "ret_5d": _float(s.ret_5d),
            "vs_spy_5d": _float(s.vs_spy_5d),
            "premarket": None,
        }
        for s in sectors
    ]
    return {
        "id": "sector_setup",
        "tier": "full" if rows else "suppressed",
        "note": None if rows else "No sectors in the book.",
        "rows": rows,
    }


def _exposure_check(flags: list[FlagCandidate]) -> dict[str, object]:
    """§6 Exposure check — the flags' documented home (docs/05 §6), silent by
    default. The figures live in the top-level ``flags[]``; the section carries
    the tier so a renderer knows whether to draw the heading at all."""
    return {
        "id": "exposure_check",
        "tier": "full" if flags else "suppressed",
        "note": None if flags else _NO_FLAGS,
        "rows": [],
    }


def _float(value: Decimal | None) -> float | None:
    """Ratios cross into the contract as floats (they are display figures, off
    the money path — the same rule ``assemble.py`` applies to day_return)."""
    return float(value) if value is not None else None


# --- Database layer -------------------------------------------------------

_READ_HOLDINGS = text("""
    SELECT symbol, status FROM holdings WHERE user_id = :user_id
""")

_READ_SECTORS = text("""
    SELECT id, name, benchmark_symbol FROM sectors
    WHERE user_id = :user_id ORDER BY sort_order, name
""")

_READ_EVENTS = text("""
    SELECT symbol, event_type, occurs_at, label FROM events
    WHERE occurs_at >= :session_date AND occurs_at <= :horizon
""")

# Chronological closes for one symbol, oldest first, ending at the prior session.
_READ_CLOSES = text("""
    SELECT c FROM (
        SELECT session_date, c
        FROM bars_daily
        WHERE symbol = :symbol AND session_date <= :prior_session
        ORDER BY session_date DESC
        LIMIT :depth
    ) recent
    ORDER BY session_date
""")

_UPSERT_BRIEF = text("""
    INSERT INTO briefs (user_id, session_date, kind, schema_version, body)
    VALUES (:user_id, :session_date, 'open', :schema_version, CAST(:body AS jsonb))
    ON CONFLICT (user_id, session_date, kind) DO UPDATE
        SET body = EXCLUDED.body,
            schema_version = EXCLUDED.schema_version,
            created_at = now()
""")


def read_open_inputs(
    conn: Connection, user_id: str, session_date: date, prior_session: date
) -> tuple[list[CalendarEvent], list[SectorSetup], dict[str, str]]:
    """Every cached read the open brief needs, in one place."""
    holdings = {
        row["symbol"]: str(row["status"])
        for row in conn.execute(_READ_HOLDINGS, {"user_id": user_id}).mappings()
    }
    events = _read_events(conn, session_date)
    sectors = _read_sectors(conn, user_id, prior_session)
    return events, sectors, holdings


def _read_events(conn: Connection, session_date: date) -> list[CalendarEvent]:
    horizon = session_date.fromordinal(session_date.toordinal() + _CALENDAR_WINDOW_DAYS)
    return [
        CalendarEvent(
            symbol=row["symbol"],
            event_type=str(row["event_type"]),
            occurs_at=row["occurs_at"],
            # Pre-M14 rows (the M7 lockup seeds) have no label; fall back to
            # something readable rather than rendering an empty cell.
            label=row["label"] or _fallback_label(row["symbol"], str(row["event_type"])),
        )
        for row in conn.execute(
            _READ_EVENTS, {"session_date": session_date, "horizon": horizon}
        ).mappings()
    ]


def _fallback_label(symbol: str | None, event_type: str) -> str:
    pretty = event_type.replace("_", "-")
    return f"{symbol} {pretty}" if symbol else pretty


def _read_sectors(
    conn: Connection, user_id: str, prior_session: date
) -> list[SectorSetup]:
    from worker.constants import BENCHMARK_SYMBOL

    spy_5d = trailing_return(
        _read_closes(conn, BENCHMARK_SYMBOL, prior_session), _SECTOR_WINDOW
    )

    out: list[SectorSetup] = []
    for row in conn.execute(_READ_SECTORS, {"user_id": user_id}).mappings():
        benchmark = row["benchmark_symbol"]
        ret_5d = (
            trailing_return(_read_closes(conn, benchmark, prior_session), _SECTOR_WINDOW)
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
                    ret_5d - spy_5d if ret_5d is not None and spy_5d is not None else None
                ),
            )
        )
    return out


def _read_closes(conn: Connection, symbol: str, prior_session: date) -> list[Decimal]:
    rows = conn.execute(
        _READ_CLOSES,
        {"symbol": symbol, "prior_session": prior_session, "depth": _SECTOR_WINDOW + 1},
    ).all()
    return [Decimal(str(r[0])) for r in rows]


def assemble_open_and_store(
    conn: Connection,
    user_id: str,
    session_date: date,
    *,
    prior_session: date,
    generated_at: datetime,
    narrator: object | None = None,
) -> BriefObject:
    """Read the cached inputs, assemble, narrate, and upsert into ``briefs``.

    Always returns an object — there is no skip gate (docs/05). Idempotent on
    ``(user_id, session_date, kind)``. Unlike the close path this never calls
    ``compute_and_store``, so an open brief cannot fail on a missing bar.
    """
    from worker.narrate import narrate_open_and_apply

    events, sectors, holdings = read_open_inputs(conn, user_id, session_date, prior_session)

    symbols = sorted(holdings)
    surfaced = surface_open_flags(conn, user_id, session_date, symbols, prior_session)

    obj = assemble_open(
        events=events,
        sectors=sectors,
        flags=surfaced,
        holdings=holdings,
        user_id=user_id,
        session_date=session_date,
        prior_session=prior_session,
        generated_at=generated_at,
    )
    # Stage ⑤ — non-fatal by construction (D19): a failed call ships tables-only.
    obj = narrate_open_and_apply(obj, narrator)  # type: ignore[arg-type]

    _store(conn, obj)

    from worker.flags import record_flags

    record_flags(conn, user_id, session_date, surfaced)
    return obj


def surface_open_flags(
    conn: Connection,
    user_id: str,
    session_date: date,
    symbols: list[str],
    prior_session: date,
) -> list[FlagCandidate]:
    """§6's flags, computed from prior-close weights rather than a P&L result.

    Weights come straight from ``bars_daily`` × ``lots`` — the same arithmetic
    ``compute`` does, minus every P&L figure the open brief must not carry.
    """
    from worker.flags import surface_flags

    weights = _prior_close_weights(conn, user_id, prior_session)
    return surface_flags(
        conn, user_id, session_date, symbols=symbols, name_weights=weights
    )


_READ_POSITION_VALUES = text("""
    SELECT h.symbol AS symbol, sum(l.shares * b.c) AS value
    FROM lots l
    JOIN holdings h ON h.id = l.holding_id
    JOIN bars_daily b ON b.symbol = h.symbol AND b.session_date = :prior_session
    WHERE l.user_id = :user_id
      AND l.opened_on <= :prior_session
      AND (l.closed_on IS NULL OR l.closed_on > :prior_session)
    GROUP BY h.symbol
""")


def _prior_close_weights(
    conn: Connection, user_id: str, prior_session: date
) -> dict[str, Fraction]:
    """Each held name's share of book value at the prior close."""
    values = {
        str(row["symbol"]): Fraction(Decimal(str(row["value"])))
        for row in conn.execute(
            _READ_POSITION_VALUES, {"user_id": user_id, "prior_session": prior_session}
        ).mappings()
    }
    total = sum(values.values(), Fraction(0))
    if total <= 0:
        return {}
    return {symbol: value / total for symbol, value in values.items()}


def _store(conn: Connection, obj: BriefObject) -> None:
    conn.execute(
        _UPSERT_BRIEF,
        {
            "user_id": obj.user_id,
            "session_date": obj.session_date,
            "schema_version": obj.schema_version,
            "body": json.dumps(obj.model_dump(mode="json")),
        },
    )
