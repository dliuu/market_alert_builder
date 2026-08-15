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
from decimal import Decimal
from fractions import Fraction
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from contracts.brief import Id as SectionId
from contracts.brief import Tier1 as RowTier
from worker.assemble_shared import claim_dict, resolved_dict, round_bps, session_label
from worker.attribution import material_residual, read_attribution_decomp
from worker.catalysts import CatalystItem, mark_reported, read_catalysts
from worker.catalysts_section import catalysts_section
from worker.claims import (
    Claim,
    ResolvedClaim,
    emit_claims,
    resolve_due_claims,
    store_emitted_claims,
)
from worker.compute import BookMetrics, ComputeResult, PositionMetrics, compute_and_store
from worker.constants import ATTRIBUTION_MODEL_VERSION, CATALYST_MODEL_VERSION
from worker.narrate import Narrator, narrate_and_apply
from worker.tape import TapeMetrics, compute_and_store_tape

# Object shape version. Bump on any shape change and keep old renderers
# (docs/04). v1 = M4 (book + attribution). v2 = M5 (per-row `tier`, tape_quality
# section, populated `suppressed[]`). v3 = M14 (nullable `book` so the open brief
# can omit P&L, plus the §4 calendar / §5 sector-setup row fields and an optional
# row `symbol` for macro releases). v4 = M13 (attribution decomposition +
# |resid_z| salience on the close brief's attribution rows).
# D22 said M13 and M15 both bump and landing order assigns the number. M13
# landed first and took v4, so M15 is v5: the §2/§3 row fields and the
# horizon-0 morning claim. v6 = M17: the close brief's catalysts section
# (insider flow and Form 144 supply signals) and its row fields.
SCHEMA_VERSION = 6

_BPS_PER_UNIT = 10_000

# Suppression thresholds (docs/05). `full` also fires on an RVOL spike; `brief`
# is the 0.3–1% band; below that a name is suppressed. The earnings-in-5-sessions
# and "carries news" exceptions need data that lands at M7/M8 and are not yet
# applied here.
_FULL_MOVE = Fraction(1, 100)  # > 1%
_BRIEF_MOVE = Fraction(3, 1000)  # >= 0.3%
_RVOL_SPIKE = Fraction(3, 2)  # > 1.5x
# A position this large is never folded into the roll-up line, however quiet it
# was: at this weight "it didn't move" is itself the thing you need to see, and
# a name you can't see is one you can't reconsider. It earns a bare row only —
# promoting it to `full` would hand it a tape row, make it claim-eligible, and
# defeat the quiet-session skip, none of which a flat day has earned.
_ALWAYS_SHOW_WEIGHT = Fraction(15, 100)  # > 15% of book value


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
    decomp: dict[str, dict[str, object]] | None = None,
    catalysts: list[CatalystItem] | None = None,
) -> BriefObject:
    """Build a validated ``BriefObject`` from computed metrics.

    ``closes`` maps each held symbol to its session close (dollars); ``tape``
    carries RVOL / range position per symbol. ``claims`` are emitted this
    session, ``resolved`` are prior ones graded now (M6). ``generated_at`` is
    injected (never ``datetime.now()``) so the object is deterministic and
    snapshot-testable. ``decomp`` is the per-symbol attribution decomposition
    (M13), read from the DB by ``assemble_and_store`` — ``assemble`` itself is
    pure and never queries it.
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

    decomp = decomp or {}
    shown, suppressed = _tier_positions(result, tape, decomp)

    sections: list[dict[str, object]] = [
        _attribution(shown, closes, decomp),
        _tape_quality(shown, tape),
    ]
    # Catalysts is post-close data — Form 4s and 144s land after the bell — so
    # it belongs to the close brief only. The open brief is assembled elsewhere
    # (assemble_open.py) and never carries it.
    if kind == "close":
        sections.append(
            catalysts_section(catalysts or [], held=[p.symbol for p in result.positions])
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
        "sections": sections,
        "flags": list(flags or []),
        "claims": [claim_dict(c, user_id, session_date, kind) for c in claims or []],
        "resolved_claims": [resolved_dict(r) for r in resolved or []],
        "suppressed": sorted(suppressed),
        "data_quality": {"missing": missing or [], "stale": stale or []},
    }
    # model_validate enforces the contract (extra='forbid', required keys, types).
    return BriefObject.model_validate(payload)


def _tier_positions(
    result: ComputeResult, tape: dict[str, TapeMetrics], decomp: dict[str, dict[str, object]]
) -> tuple[list[tuple[PositionMetrics, str]], list[str]]:
    """Tier every held name. Suppressed names leave the rows and become one
    roll-up line; the survivors keep their tier for narration (M8), tape, and
    claim emission (M6). A residual-material name (M13) is always full-tier,
    even on a flat raw move."""
    shown: list[tuple[PositionMetrics, str]] = []
    suppressed: list[str] = []
    for p in result.positions:
        rvol = tape[p.symbol].rvol if p.symbol in tape else None
        resid_z = cast("float | None", decomp.get(p.symbol, {}).get("resid_z"))
        tier = _tier(p.day_return, rvol, resid_z, p.weight)
        if tier == "suppressed":
            suppressed.append(p.symbol)
        else:
            shown.append((p, tier))
    return shown, suppressed


def _tier(
    day_return: Fraction | None,
    rvol: Fraction | None,
    resid_z: float | None = None,
    weight: Fraction | None = None,
) -> str:
    """Classify a name into full / brief / suppressed (docs/05). A residual-
    material move (M13 §2) is always full, even on a flat raw move or no RVOL
    spike. A position over ``_ALWAYS_SHOW_WEIGHT`` is never suppressed."""
    if material_residual(resid_z):
        return "full"
    moved = abs(day_return) if day_return is not None else Fraction(0)
    if moved > _FULL_MOVE or (rvol is not None and rvol > _RVOL_SPIKE):
        return "full"
    if moved >= _BRIEF_MOVE:
        return "brief"
    # A weight floor, applied last so it only ever rescues from suppression.
    if weight is not None and weight > _ALWAYS_SHOW_WEIGHT:
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
    day_bps = round_bps(book.day_bps)
    assert day_bps is not None  # precondition: assemble() checks book.day_bps first
    out: dict[str, int | float] = {
        "value_cents": book.value_cents,
        "day_pnl_cents": book.day_pnl_cents,
        "day_bps": day_bps,
        "total_pnl_cents": book.total_pnl_cents,
    }
    if book.total_pct is not None:
        out["total_pct"] = float(book.total_pct)
    vs_spy = round_bps(book.vs_spy_bps)
    if vs_spy is not None:
        out["vs_spy_bps"] = vs_spy
    return out


def _attribution(
    shown: list[tuple[PositionMetrics, str]],
    closes: dict[str, Decimal],
    decomp: dict[str, dict[str, object]],
) -> dict[str, object]:
    rows = [
        {
            "symbol": p.symbol,
            "tier": tier,
            "close": float(closes[p.symbol]) if p.symbol in closes else None,
            "day_return": float(p.day_return) if p.day_return is not None else None,
            "day_pnl_cents": p.day_pnl_cents,
            "contribution_bps": round_bps(p.contribution_bps),
            "total_pnl_cents": p.total_pnl_cents,
            "total_pct": _row_total_pct(p),
            "market_bps": decomp.get(p.symbol, {}).get("market_bps"),
            "theme_bps": decomp.get(p.symbol, {}).get("theme_bps"),
            "resid_bps": decomp.get(p.symbol, {}).get("resid_bps"),
            "resid_z": decomp.get(p.symbol, {}).get("resid_z"),
            "provisional": decomp.get(p.symbol, {}).get("provisional"),
        }
        for p, tier in shown
    ]
    rows.sort(key=_salience_sort_key, reverse=True)
    return {"id": "attribution", "tier": "full", "note": None, "rows": rows}


def _salience_sort_key(row: dict[str, object]) -> tuple[bool, float, float]:
    """Rank attribution rows: residual-material names first by |resid_z|, then
    everything else by |contribution_bps|.

    The earlier key was ``(resid_z is not None, |resid_z|)``, which made *any*
    decomposed row outrank *every* undecomposed one — so a name with a trivial
    |resid_z| led over a name that actually moved the book but had no fit. Being
    undecomposed is a recurring state, not an edge case: a position opened before
    the weekly refit, a recent IPO, a name in no theme. Sorting those last hides
    exactly the row the reader needs, and does it silently.

    Materiality is the existing ``material_residual`` threshold rather than a
    fresh one, and the two scales are never compared — a z-score orders only
    against other z-scores, bps only against bps. When idiosyncrasy is unknown or
    immaterial, the honest fallback is "the largest contributor leads".
    """
    resid_z = cast("float | None", row.get("resid_z"))
    contribution = cast("int | None", row.get("contribution_bps"))
    material = material_residual(resid_z)
    return (
        material,
        abs(resid_z) if material and resid_z is not None else 0.0,
        abs(contribution) if contribution is not None else 0.0,
    )


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
    when = session_label(session_date)
    pct = float(book.day_bps) / 100 if book.day_bps is not None else 0.0
    dollars = book.day_pnl_cents / 100
    return f"{label} · {when} — book {pct:+.1f}% ({_signed_dollars(dollars)})"


def _signed_dollars(dollars: float) -> str:
    sign = "+" if dollars >= 0 else "-"
    return f"{sign}${abs(dollars):,.2f}"


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
    decomp = read_attribution_decomp(conn, symbols, session_date, ATTRIBUTION_MODEL_VERSION)

    # Grade prior claims first — that's independent of whether this brief sends.
    resolved = resolve_due_claims(conn, user_id, session_date)
    shown, _ = _tier_positions(result, tape, decomp)
    emitted = emit_claims(shown, result.benchmark_return)

    from datetime import UTC

    # No flags here since M14. §6 exposure check is the *open* brief's section
    # (docs/05), and `flags.last_seen` is a single weekly clock — if both briefs
    # surfaced flags, the 08:15 fire would spend the budget and the 16:45 one
    # would silently render nothing. D18 only put them here because the open
    # brief didn't exist yet. `flags[]` stays in the shape, empty.
    catalysts = (
        read_catalysts(conn, user_id, symbols, session_date,
                       model_version=CATALYST_MODEL_VERSION)
        if kind == "close" else []
    )

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
        decomp=decomp,
        catalysts=catalysts,
    )
    if close_brief_should_skip(obj):
        # A quiet session still resolved its due claims (above) but emits none.
        # Catalysts are deliberately *not* marked reported here: a brief that
        # never sends must not consume anyone's report-once budget, or the
        # signal would arrive already-condensed on the next session that does.
        return None
    # Stage ⑤: prose is added only to briefs that actually send, and never at
    # the cost of the send — a failed Claude call returns the object unchanged.
    obj = narrate_and_apply(obj, narrator)
    _store_brief(conn, obj)
    store_emitted_claims(conn, user_id, obj.brief_id, session_date, emitted)
    mark_reported(conn, user_id, catalysts, now=datetime.now(UTC))
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
