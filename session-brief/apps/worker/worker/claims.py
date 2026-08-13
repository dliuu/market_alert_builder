"""Stage ④ accountability loop: emit falsifiable claims, then grade prior ones
from the tape (docs/05). M6 implements the one type that's fully mechanical over
``bars_daily`` — ``relative_strength`` (a name out/under-performing the book
benchmark) — with a clean seam (`claim_type` dispatch) for the event-driven
types (catalyst_pending, supply_overhang) once that data lands at M7.

Emission is pure. Resolution reads bars to grade, using ``bars_daily`` itself as
the session clock (the H-th bar after emission), which honours "trading days
come from real sessions" (invariant 7) without a hardcoded calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.compute import PositionMetrics
from worker.constants import ATTRIBUTION_MODEL_VERSION

# A full-tier name must beat/lag the benchmark by more than this (1d relative)
# to be worth a claim. A tunable placeholder for the mechanism (D17).
_REL_STRENGTH_THRESHOLD = Fraction(1, 100)
_DEFAULT_HORIZON = 1


@dataclass(frozen=True)
class Claim:
    symbol: str
    claim_type: str
    direction: str  # up | down | neutral
    horizon_sessions: int


@dataclass(frozen=True)
class ResolvedClaim:
    id: str
    symbol: str
    claim_type: str
    direction: str
    horizon_sessions: int
    outcome: str  # correct | wrong


# --- Emission (pure) ------------------------------------------------------


def emit_claims(
    shown: list[tuple[PositionMetrics, str]], benchmark_return: Fraction | None
) -> list[Claim]:
    """Claims this session commits to. Only full-tier names, only when their
    1-day relative strength clears the threshold."""
    if benchmark_return is None:
        return []
    out: list[Claim] = []
    for position, tier in shown:
        if tier != "full" or position.day_return is None:
            continue
        rel = position.day_return - benchmark_return
        if abs(rel) <= _REL_STRENGTH_THRESHOLD:
            continue
        out.append(
            Claim(
                symbol=position.symbol,
                claim_type="relative_strength",
                direction="up" if rel > 0 else "down",
                horizon_sessions=_DEFAULT_HORIZON,
            )
        )
    return out


# --- Persistence + resolution (DB) ----------------------------------------

_INSERT = text("""
    INSERT INTO claims (user_id, brief_id, symbol, claim_type, direction,
                        horizon_sessions, session_date)
    VALUES (:user_id, :brief_id, :symbol, :claim_type, :direction,
            :horizon_sessions, :session_date)
    ON CONFLICT (user_id, symbol, claim_type, session_date) DO NOTHING
""")

_READ_UNRESOLVED = text("""
    SELECT id, symbol, claim_type, direction, horizon_sessions, session_date
    FROM claims
    WHERE user_id = :user_id AND outcome IS NULL AND session_date < :session_date
""")

# The horizon-th session strictly after the emit session, for this symbol.
_RESOLVE_SESSION = text("""
    SELECT session_date FROM bars_daily
    WHERE symbol = :symbol AND session_date > :emitted_on
    ORDER BY session_date
    LIMIT 1 OFFSET :offset
""")

# Realized residual for the graded session (M13 §3: grade against the SIGN of
# resid_bps, not the raw sym-vs-benchmark return — beta shouldn't earn credit).
_RESID_ON = text("""
    SELECT resid_bps FROM attribution
    WHERE symbol = :symbol AND trade_date = :d AND model_version = :mv
""")

_UPDATE_RESOLVED = text("""
    UPDATE claims
    SET outcome = :outcome, resolved_at = now(), resolved_session = :resolved_session,
        graded_model_version = :graded_mv
    WHERE id = :id
""")


def store_emitted_claims(
    conn: Connection, user_id: str, brief_id: str, session_date: date, claims: list[Claim]
) -> None:
    for claim in claims:
        conn.execute(
            _INSERT,
            {
                "user_id": user_id,
                "brief_id": brief_id,
                "symbol": claim.symbol,
                "claim_type": claim.claim_type,
                "direction": claim.direction,
                "horizon_sessions": claim.horizon_sessions,
                "session_date": session_date,
            },
        )


def resolve_due_claims(
    conn: Connection, user_id: str, session_date: date
) -> list[ResolvedClaim]:
    """Grade every unresolved claim whose horizon window has closed as of
    ``session_date``, persist the outcome, and return them. Idempotent — an
    already-resolved claim is never regraded."""
    rows = conn.execute(
        _READ_UNRESOLVED, {"user_id": user_id, "session_date": session_date}
    ).mappings().all()

    resolved: list[ResolvedClaim] = []
    for row in rows:
        resolve_on = _resolve_session(
            conn, row["symbol"], row["session_date"], row["horizon_sessions"]
        )
        if resolve_on is None or resolve_on > session_date:
            continue  # the horizon hasn't elapsed yet
        graded = _grade(conn, row["symbol"], row["direction"], resolve_on)
        if graded is None:
            continue  # can't grade (no realized residual yet) — leave for next run
        outcome, graded_mv = graded
        conn.execute(
            _UPDATE_RESOLVED,
            {
                "id": row["id"],
                "outcome": outcome,
                "resolved_session": resolve_on,
                "graded_mv": graded_mv,
            },
        )
        resolved.append(
            ResolvedClaim(
                id=str(row["id"]),
                symbol=row["symbol"],
                claim_type=row["claim_type"],
                direction=row["direction"],
                horizon_sessions=row["horizon_sessions"],
                outcome=outcome,
            )
        )
    return resolved


def _resolve_session(
    conn: Connection, symbol: str, emitted_on: date, horizon: int
) -> date | None:
    return conn.execute(
        _RESOLVE_SESSION, {"symbol": symbol, "emitted_on": emitted_on, "offset": horizon - 1}
    ).scalar()


def _grade(
    conn: Connection, symbol: str, direction: str, resolve_on: date
) -> tuple[str, int] | None:
    """Grade the claimed direction against the SIGN of the realized residual
    (M13 §3): an overnight/relative call shouldn't get credit for market beta.
    Returns (outcome, model_version), or None when no residual is stored yet."""
    row = conn.execute(
        _RESID_ON,
        {"symbol": symbol, "d": resolve_on, "mv": ATTRIBUTION_MODEL_VERSION},
    ).mappings().first()
    if row is None or row["resid_bps"] is None:
        return None
    resid = float(row["resid_bps"])
    if direction == "up":
        return ("correct" if resid > 0 else "wrong", ATTRIBUTION_MODEL_VERSION)
    if direction == "down":
        return ("correct" if resid < 0 else "wrong", ATTRIBUTION_MODEL_VERSION)
    return None
