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
from worker.constants import BENCHMARK_SYMBOL
from worker.premarket import PremarketQuote

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


def emit_premarket_gap(quotes: list[PremarketQuote]) -> list[Claim]:
    """The morning call (M15): a name gapping pre-market is claimed to hold that
    relative direction into the close, **horizon 0** — resolved by that same
    session's close brief. This is the same-day open→close loop D16b built the
    engine for and could not run until an open brief emitted.

    The threshold is §3's: if the move isn't worth a row, it isn't worth a
    falsifiable call.
    """
    from worker.premarket import clears_threshold, pre_pct

    out: list[Claim] = []
    for quote in quotes:
        if not clears_threshold(quote):
            continue
        pct = pre_pct(quote)
        if pct is None or pct == 0:
            continue
        out.append(
            Claim(
                symbol=quote.symbol,
                claim_type="premarket_gap",
                direction="up" if pct > 0 else "down",
                horizon_sessions=0,
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

# `<=`, not `<`: a horizon-0 morning claim is due the session it was emitted
# on. Horizon-1 claims emitted today are still excluded — not by this filter but
# by `_resolve_session`, which places their resolution on the *next* session.
_READ_UNRESOLVED = text("""
    SELECT id, symbol, claim_type, direction, horizon_sessions, session_date
    FROM claims
    WHERE user_id = :user_id AND outcome IS NULL AND session_date <= :session_date
""")

# The horizon-th session strictly after the emit session, for this symbol.
_RESOLVE_SESSION = text("""
    SELECT session_date FROM bars_daily
    WHERE symbol = :symbol AND session_date > :emitted_on
    ORDER BY session_date
    LIMIT 1 OFFSET :offset
""")

# Same-session day returns for the name and the benchmark (LAG for prev close).
# Horizon >= 1 only — see `_grade`'s horizon-0 branch for why horizon 0 cannot
# reuse this close-to-close base.
_RETURNS = text("""
    SELECT symbol, c, prev_c FROM (
        SELECT symbol, session_date, c,
               LAG(c) OVER (PARTITION BY symbol ORDER BY session_date) AS prev_c
        FROM bars_daily
    ) ranked
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
""")

# Horizon 0's base: the *same* session's own open and close (I1, M15 review).
_RETURNS_OPEN_CLOSE = text("""
    SELECT symbol, o, c FROM bars_daily
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
""")

_UPDATE_RESOLVED = text("""
    UPDATE claims
    SET outcome = :outcome, resolved_at = now(), resolved_session = :resolved_session
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
        outcome = _grade(
            conn, row["symbol"], row["direction"], resolve_on, row["horizon_sessions"]
        )
        if outcome is None:
            continue  # can't grade (missing benchmark bar) — leave for next run
        conn.execute(
            _UPDATE_RESOLVED, {"id": row["id"], "outcome": outcome, "resolved_session": resolve_on}
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


# Horizon 0 resolves on the emit session itself — but only once that session has
# a bar. Before the close there is nothing to grade, so the claim waits.
_SAME_SESSION = text("""
    SELECT session_date FROM bars_daily
    WHERE symbol = :symbol AND session_date = :emitted_on
""")


def _resolve_session(
    conn: Connection, symbol: str, emitted_on: date, horizon: int
) -> date | None:
    if horizon == 0:
        return conn.execute(
            _SAME_SESSION, {"symbol": symbol, "emitted_on": emitted_on}
        ).scalar()
    return conn.execute(
        _RESOLVE_SESSION, {"symbol": symbol, "emitted_on": emitted_on, "offset": horizon - 1}
    ).scalar()


def _grade(
    conn: Connection, symbol: str, direction: str, resolve_on: date, horizon: int
) -> str | None:
    """Dispatch to the grader the claim's own shape calls for.

    The two arms answer different questions, and keeping them as separate
    functions rather than branches inside one body is deliberate:
    ``_grade_relative`` is the seam M13's residual grading replaces whole, and
    its docstring says why horizon 0 does not follow it there.
    """
    if horizon == 0:
        return _grade_open_close(conn, symbol, direction, resolve_on)
    return _grade_relative(conn, symbol, direction, resolve_on)


def _grade_open_close(
    conn: Connection, symbol: str, direction: str, resolve_on: date
) -> str | None:
    """Horizon 0 (the morning ``premarket_gap`` claim): open→close on the emit
    session itself, against the benchmark.

    ``emit_premarket_gap`` takes its direction from
    ``extended_last / prev_close - 1`` — session D-1's close as the base — and a
    close-to-close base (D-1's close vs. D's close) *contains that same gap as a
    sub-interval*. A name that gaps up pre-market and then fully fades intraday
    would still grade "correct", because the overnight gap alone would carry the
    close-to-close return past the benchmark's. The gap is what the claim is
    *about*, so it cannot also be what grades it. Open→close excludes the gap and
    grades only what happened during the session the claim was made about.
    """
    returns = {
        row["symbol"]: _day_return(row["c"], row["o"])
        for row in conn.execute(
            _RETURNS_OPEN_CLOSE,
            {"session_date": resolve_on, "symbols": [symbol, BENCHMARK_SYMBOL]},
        ).mappings()
    }
    return _verdict(returns.get(symbol), returns.get(BENCHMARK_SYMBOL), direction)


def _grade_relative(
    conn: Connection, symbol: str, direction: str, resolve_on: date
) -> str | None:
    """Horizon >= 1: close-to-close relative strength, exactly as M6/D17 built it.

    **This function is the M13 seam.** M13 (upstream) re-points horizon >= 1
    grading at the *sign of the realized residual* (`attribution.resid_bps`), so
    that beta earns no credit — a name that rose only because the market rose has
    not vindicated a call about that name — and stamps
    ``claims.graded_model_version``. That change replaces this function's body
    and nothing else: the dispatch above, the horizon-0 grader, and
    ``emit_premarket_gap`` are all unaffected by it.

    Horizon 0 deliberately does **not** follow it there. The morning claim is
    emitted at 08:15, and that session's attribution row does not exist until the
    PM score runs after the close — there is nothing to residualize against at
    emission time, and the grade has to land in that same evening's close brief.
    It is also explicitly a price call ("this gap holds into the close"), not a
    factor-adjusted one, so residualizing it would grade a different claim than
    the one the brief made to the reader.
    """
    returns = {
        row["symbol"]: _day_return(row["c"], row["prev_c"])
        for row in conn.execute(
            _RETURNS, {"session_date": resolve_on, "symbols": [symbol, BENCHMARK_SYMBOL]}
        ).mappings()
    }
    return _verdict(returns.get(symbol), returns.get(BENCHMARK_SYMBOL), direction)


def _verdict(
    sym: Fraction | None, bench: Fraction | None, direction: str
) -> str | None:
    """correct/wrong for the claimed direction, or None when the data to judge
    isn't there. Shared by both graders so a change to what counts as vindicated
    can't drift between them."""
    if sym is None or bench is None:
        return None
    rel = sym - bench
    if direction == "up":
        return "correct" if rel > 0 else "wrong"
    if direction == "down":
        return "correct" if rel < 0 else "wrong"
    return None


def _day_return(c: object, prev_c: object) -> Fraction | None:
    if prev_c is None:
        return None
    prev = Fraction(str(prev_c))
    if prev == 0:
        return None
    return (Fraction(str(c)) - prev) / prev
