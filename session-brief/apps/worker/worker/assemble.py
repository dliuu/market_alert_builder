"""Stage ④ assemble: ComputeResult → validated BriefObject.

``assemble`` is a pure function of its inputs — no network, clock, or float on
the money path — so a frozen fixture snapshots the whole object (docs/04). It
turns the exact ``Fraction``s that stage ③ carries into the contract's display
types: basis points round to ``int``, ratios become ``float``. Money stays
integer cents end to end (invariant: never float).

M4 scope: the close brief's ``book`` totals and one ``attribution`` section.
Suppression tiers (M5), tape quality (M5), claims (M6), flags (M7) and narration
(M8) are deliberately empty here; the schema allows their arrays to be empty and
``one_thing``/``why`` to be null.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker.compute import BookMetrics, ComputeResult, PositionMetrics, compute_and_store

# The first shipped object shape. Bump on any shape change and keep old
# renderers (docs/04). The "3" in the docs/04 example is illustrative, not
# history — there was no v1/v2.
SCHEMA_VERSION = 1

_BPS_PER_UNIT = 10_000


def assemble(
    result: ComputeResult,
    closes: dict[str, Decimal],
    *,
    user_id: str,
    session_date: date,
    kind: str,
    generated_at: datetime,
    missing: list[str] | None = None,
    stale: list[str] | None = None,
) -> BriefObject:
    """Build a validated ``BriefObject`` from computed metrics.

    ``closes`` maps each held symbol to its session close (dollars) — the one
    figure the attribution rows need that ``ComputeResult`` doesn't carry.
    ``generated_at`` is injected (never ``datetime.now()``) so the object is
    deterministic and snapshot-testable.
    """
    if kind not in ("open", "close"):
        raise ValueError(f"unknown brief kind {kind!r}")

    book = result.book
    if book.day_bps is None:
        # A book that opened entirely today has no prior value and no day return.
        # Graceful degradation is deferred to M5; M4 asserts the happy path.
        raise ValueError(
            f"cannot assemble {kind} brief for {session_date}: book day_bps is undefined "
            "(no prior-close value). Graceful degradation lands in M5."
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "brief_id": f"{user_id}-{session_date.isoformat()}-{kind}",
        "user_id": user_id,
        "session_date": session_date.isoformat(),
        "kind": kind,
        "generated_at": generated_at.isoformat(),
        "subject": _subject(kind, session_date, book),
        "one_thing": None,  # narration, M8
        "book": _book(book),
        "sections": [_attribution(result.positions, closes)],
        "flags": [],  # M7
        "claims": [],  # M6
        "resolved_claims": [],  # M6
        "suppressed": [],  # M5
        "data_quality": {"missing": missing or [], "stale": stale or []},
    }
    # model_validate enforces the contract (extra='forbid', required keys, types).
    return BriefObject.model_validate(payload)


def _book(book: BookMetrics) -> dict[str, int | float]:
    day_bps = _round_bps(book.day_bps)
    assert day_bps is not None  # precondition: assemble() checks book.day_bps first
    out: dict[str, int | float] = {
        "value_cents": book.value_cents,
        "day_pnl_cents": book.day_pnl_cents,
        "day_bps": day_bps,
        "total_pnl_cents": book.total_pnl_cents,
    }
    if book.total_pct is not None:
        out["total_pct"] = float(book.total_pct)
    vs_spy = _round_bps(book.vs_spy_bps)
    if vs_spy is not None:
        out["vs_spy_bps"] = vs_spy
    return out


def _attribution(positions: list[PositionMetrics], closes: dict[str, Decimal]) -> dict[str, object]:
    rows = [
        {
            "symbol": p.symbol,
            "close": float(closes[p.symbol]) if p.symbol in closes else None,
            "day_return": float(p.day_return) if p.day_return is not None else None,
            "day_pnl_cents": p.day_pnl_cents,
            "contribution_bps": _round_bps(p.contribution_bps),
            "total_pnl_cents": p.total_pnl_cents,
            "total_pct": _row_total_pct(p),
        }
        for p in positions
    ]
    return {"id": "attribution", "tier": "full", "note": None, "rows": rows}


def _row_total_pct(p: PositionMetrics) -> float | None:
    if p.total_cost_cents <= 0:
        return None
    return float(Fraction(p.total_pnl_cents, p.total_cost_cents))


def _subject(kind: str, session_date: date, book: BookMetrics) -> str:
    label = kind.capitalize()
    when = f"{session_date:%a} {session_date:%b} {session_date.day}"
    pct = float(book.day_bps) / 100 if book.day_bps is not None else 0.0
    dollars = book.day_pnl_cents / 100
    return f"{label} · {when} — book {pct:+.1f}% ({_signed_dollars(dollars)})"


def _signed_dollars(dollars: float) -> str:
    sign = "+" if dollars >= 0 else "-"
    return f"{sign}${abs(dollars):,.2f}"


def _round_bps(value: Fraction | None) -> int | None:
    """Round an exact bps Fraction to the contract's integer. The exact
    sum identity (Σ contribution_bps == day_bps) lives in stage ③ over
    Fractions; the rounded integers here are for display and need not re-sum
    (documented as D15)."""
    if value is None:
        return None
    return int(
        (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


# --- Database layer -------------------------------------------------------

_READ_CLOSES = text("""
    SELECT symbol, c FROM bars_daily
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
""")

_UPSERT_BRIEF = text("""
    INSERT INTO briefs (user_id, session_date, kind, schema_version, body)
    VALUES (:user_id, :session_date, :kind, :schema_version, CAST(:body AS jsonb))
    ON CONFLICT (user_id, session_date, kind) DO UPDATE
        SET body = EXCLUDED.body,
            schema_version = EXCLUDED.schema_version,
            created_at = now()
""")


def assemble_and_store(
    conn: Connection, user_id: str, session_date: date, kind: str = "close"
) -> BriefObject:
    """Compute metrics, assemble the object, and upsert it into ``briefs``.
    Idempotent on ``(user_id, session_date, kind)``: re-running replaces the row
    rather than duplicating it."""
    result = compute_and_store(conn, user_id, session_date)
    symbols = [p.symbol for p in result.positions]
    closes = _read_closes(conn, symbols, session_date)

    from datetime import UTC

    obj = assemble(
        result,
        closes,
        user_id=user_id,
        session_date=session_date,
        kind=kind,
        generated_at=datetime.now(UTC),
    )
    _store_brief(conn, obj)
    return obj


def _read_closes(conn: Connection, symbols: list[str], session_date: date) -> dict[str, Decimal]:
    if not symbols:
        return {}
    rows = conn.execute(
        _READ_CLOSES, {"session_date": session_date, "symbols": symbols}
    ).mappings().all()
    return {row["symbol"]: Decimal(str(row["c"])) for row in rows}


def _store_brief(conn: Connection, obj: BriefObject) -> None:
    conn.execute(
        _UPSERT_BRIEF,
        {
            "user_id": obj.user_id,
            "session_date": obj.session_date,
            "kind": obj.kind.value,
            "schema_version": obj.schema_version,
            "body": json.dumps(obj.model_dump(mode="json")),
        },
    )
