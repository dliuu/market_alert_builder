"""Stage ④ assemble: ComputeResult → validated BriefObject.

``assemble`` is a pure function of its inputs — no network, clock, or float on
the money path — so a frozen fixture snapshots the whole object (docs/04). It
turns the exact ``Fraction``s that stage ③ carries into the contract's display
types: basis points round to ``int``, ratios become ``float``. Money stays
integer cents end to end (invariant: never float).

M5 adds suppression and tape quality: assembly tiers every held name
(full / brief / suppressed) from movement + RVOL, folds the suppressed ones
into ``suppressed[]`` (rendered as one roll-up line), and emits a
``tape_quality`` section for the movers. M6 adds claims; M7 populates the
top-level ``flags[]`` (position risk + the weekly-capped correlation flag). M8
narrates in ``assemble_and_store`` (stage ⑤, non-fatal): Claude fills ``one_thing``
and the attribution rows' ``why``, or leaves them null if the call fails.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from contracts.brief import Id as SectionId
from contracts.brief import Tier1 as RowTier
from worker.claims import (
    Claim,
    ResolvedClaim,
    emit_claims,
    resolve_due_claims,
    store_emitted_claims,
)
from worker.compute import BookMetrics, ComputeResult, PositionMetrics, compute_and_store
from worker.flags import candidate_dict, record_flags, surface_flags
from worker.narrate import Narrator, narrate_and_apply
from worker.tape import TapeMetrics, compute_and_store_tape

# Object shape version. Bump on any shape change and keep old renderers
# (docs/04). v1 = M4 (book + attribution). v2 = M5 (per-row `tier`, tape_quality
# section, populated `suppressed[]`).
SCHEMA_VERSION = 2

_BPS_PER_UNIT = 10_000

# Suppression thresholds (docs/05). `full` also fires on an RVOL spike; `brief`
# is the 0.3–1% band; below that a name is suppressed. The earnings-in-5-sessions
# and "carries news" exceptions need data that lands at M7/M8 and are not yet
# applied here.
_FULL_MOVE = Fraction(1, 100)  # > 1%
_BRIEF_MOVE = Fraction(3, 1000)  # >= 0.3%
_RVOL_SPIKE = Fraction(3, 2)  # > 1.5x


def assemble(
    result: ComputeResult,
    closes: dict[str, Decimal],
    tape: dict[str, TapeMetrics],
    *,
    user_id: str,
    session_date: date,
    kind: str,
    generated_at: datetime,
    claims: list[Claim] | None = None,
    resolved: list[ResolvedClaim] | None = None,
    flags: list[dict[str, object]] | None = None,
    missing: list[str] | None = None,
    stale: list[str] | None = None,
) -> BriefObject:
    """Build a validated ``BriefObject`` from computed metrics.

    ``closes`` maps each held symbol to its session close (dollars); ``tape``
    carries RVOL / range position per symbol. ``claims`` are emitted this
    session, ``resolved`` are prior ones graded now (M6). ``generated_at`` is
    injected (never ``datetime.now()``) so the object is deterministic and
    snapshot-testable.
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

    shown, suppressed = _tier_positions(result, tape)

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
        "sections": [_attribution(shown, closes), _tape_quality(shown, tape)],
        "flags": list(flags or []),
        "claims": [_claim_dict(c, user_id, session_date, kind) for c in claims or []],
        "resolved_claims": [_resolved_dict(r) for r in resolved or []],
        "suppressed": sorted(suppressed),
        "data_quality": {"missing": missing or [], "stale": stale or []},
    }
    # model_validate enforces the contract (extra='forbid', required keys, types).
    return BriefObject.model_validate(payload)


def _tier_positions(
    result: ComputeResult, tape: dict[str, TapeMetrics]
) -> tuple[list[tuple[PositionMetrics, str]], list[str]]:
    """Tier every held name. Suppressed names leave the rows and become one
    roll-up line; the survivors keep their tier for narration (M8), tape, and
    claim emission (M6)."""
    shown: list[tuple[PositionMetrics, str]] = []
    suppressed: list[str] = []
    for p in result.positions:
        rvol = tape[p.symbol].rvol if p.symbol in tape else None
        tier = _tier(p.day_return, rvol)
        if tier == "suppressed":
            suppressed.append(p.symbol)
        else:
            shown.append((p, tier))
    return shown, suppressed


def _claim_dict(claim: Claim, user_id: str, session_date: date, kind: str) -> dict[str, object]:
    # Emitted claims have no DB id yet; a deterministic slug identifies them.
    return {
        "id": f"{user_id}-{session_date.isoformat()}-{kind}-{claim.symbol}-{claim.claim_type}",
        "symbol": claim.symbol,
        "type": claim.claim_type,
        "direction": claim.direction,
        "horizon_sessions": claim.horizon_sessions,
        "outcome": None,
    }


def _resolved_dict(resolved: ResolvedClaim) -> dict[str, object]:
    return {
        "id": resolved.id,
        "symbol": resolved.symbol,
        "type": resolved.claim_type,
        "direction": resolved.direction,
        "horizon_sessions": resolved.horizon_sessions,
        "outcome": resolved.outcome,
    }


def _tier(day_return: Fraction | None, rvol: Fraction | None) -> str:
    """Classify a name into full / brief / suppressed (docs/05)."""
    moved = abs(day_return) if day_return is not None else Fraction(0)
    if moved > _FULL_MOVE or (rvol is not None and rvol > _RVOL_SPIKE):
        return "full"
    if moved >= _BRIEF_MOVE:
        return "brief"
    return "suppressed"


def close_brief_should_skip(obj: BriefObject) -> bool:
    """A close brief is skipped entirely when nothing was a full-tier mover
    (docs/05). Brief-tier names alone don't warrant a send; the open brief
    (not yet built) always sends regardless."""
    if obj.kind.value != "close":
        return False
    attribution = next((s for s in obj.sections if s.id is SectionId.attribution), None)
    if attribution is None:
        return True
    return not any(r.tier is RowTier.full for r in attribution.rows)


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


def _attribution(
    shown: list[tuple[PositionMetrics, str]], closes: dict[str, Decimal]
) -> dict[str, object]:
    rows = [
        {
            "symbol": p.symbol,
            "tier": tier,
            "close": float(closes[p.symbol]) if p.symbol in closes else None,
            "day_return": float(p.day_return) if p.day_return is not None else None,
            "day_pnl_cents": p.day_pnl_cents,
            "contribution_bps": _round_bps(p.contribution_bps),
            "total_pnl_cents": p.total_pnl_cents,
            "total_pct": _row_total_pct(p),
        }
        for p, tier in shown
    ]
    return {"id": "attribution", "tier": "full", "note": None, "rows": rows}


def _tape_quality(
    shown: list[tuple[PositionMetrics, str]], tape: dict[str, TapeMetrics]
) -> dict[str, object]:
    """§4 "How they traded" — RVOL and range position for the full-tier movers.
    Brief-tier names get a bare attribution row but no tape detail; if there are
    no movers, the whole section is suppressed."""
    rows = [
        {
            "symbol": p.symbol,
            "rvol": _tape_float(tape, p.symbol, "rvol"),
            "range_position": _tape_float(tape, p.symbol, "range_position"),
        }
        for p, tier in shown
        if tier == "full"
    ]
    tier = "full" if rows else "suppressed"
    return {"id": "tape_quality", "tier": tier, "note": None, "rows": rows}


def _tape_float(tape: dict[str, TapeMetrics], symbol: str, field: str) -> float | None:
    if symbol not in tape:
        return None
    value: Fraction | None = getattr(tape[symbol], field)
    return float(value) if value is not None else None


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
    conn: Connection,
    user_id: str,
    session_date: date,
    kind: str = "close",
    *,
    narrator: Narrator | None = None,
) -> BriefObject | None:
    """Compute metrics, assemble the object, narrate it, and upsert it into
    ``briefs``. Returns ``None`` (writing nothing) when a close brief is skipped
    because nothing was a full-tier mover. Idempotent on ``(user_id,
    session_date, kind)``: re-running replaces the row rather than duplicating
    it. ``narrator`` is off by default (tables-only); the CLI wires in the
    Claude-backed one, and narration is non-fatal regardless (M8)."""
    result = compute_and_store(conn, user_id, session_date)
    symbols = [p.symbol for p in result.positions]
    closes = _read_closes(conn, symbols, session_date)
    tape = compute_and_store_tape(conn, user_id, symbols, session_date)

    # Grade prior claims first — that's independent of whether this brief sends.
    resolved = resolve_due_claims(conn, user_id, session_date)
    shown, _ = _tier_positions(result, tape)
    emitted = emit_claims(shown, result.benchmark_return)

    # Which flags fire, after the weekly rate limit — read-only; last_seen is
    # written below only once the brief is confirmed to send (a real mention).
    surfaced = surface_flags(conn, user_id, session_date, result)

    from datetime import UTC

    obj = assemble(
        result,
        closes,
        tape,
        user_id=user_id,
        session_date=session_date,
        kind=kind,
        generated_at=datetime.now(UTC),
        claims=emitted,
        resolved=resolved,
        flags=[candidate_dict(c) for c in surfaced],
    )
    if close_brief_should_skip(obj):
        # A quiet session still resolved its due claims (above), but emits none
        # and spends no rate-limit budget (flags aren't recorded).
        return None
    # Stage ⑤: prose is added only to briefs that actually send, and never at
    # the cost of the send — a failed Claude call returns the object unchanged.
    obj = narrate_and_apply(obj, narrator)
    _store_brief(conn, obj)
    store_emitted_claims(conn, user_id, obj.brief_id, session_date, emitted)
    record_flags(conn, user_id, session_date, surfaced)
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
