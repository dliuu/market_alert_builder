"""The morning claim (M15): the open brief commits to a direction before the
bell and the *same session's* close brief grades it.

This is the loop D16b built the engine for and could not exercise — until now
every claim was horizon 1, resolved by the next day's close brief. Horizon 0 is
a real change to resolution, not a free ride on the claim_type seam:
`resolve_due_claims` read only `session_date < :session_date`, and
`_resolve_session` offset by `horizon - 1`, which is -1 at horizon 0.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.engine import Connection

from worker.claims import emit_premarket_gap
from worker.premarket import PremarketQuote


def _q(symbol: str, last: str, prev: str) -> PremarketQuote:
    return PremarketQuote(symbol, Decimal(last), 1000, Decimal(prev), Decimal("500"))


def test_a_gap_up_claims_relative_strength_today() -> None:
    (claim,) = emit_premarket_gap([_q("SNDK", "49.26", "47.32")])
    assert claim.claim_type == "premarket_gap"
    assert claim.direction == "up"
    assert claim.horizon_sessions == 0


def test_a_gap_down_claims_the_other_way() -> None:
    (claim,) = emit_premarket_gap([_q("ASTS", "34.20", "36.08")])
    assert claim.direction == "down"


def test_a_name_under_the_threshold_makes_no_claim() -> None:
    """The claim rides §3's threshold: if the move isn't worth a row, it isn't
    worth a falsifiable call."""
    assert emit_premarket_gap([_q("RKLB", "24.98", "25.00")]) == []


def test_the_same_session_close_resolves_the_morning_claim(db_conn: Connection) -> None:
    """DoD 4, end to end: emitted at 08:15, graded from that day's close."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fc"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h0@example.invalid')"), {"u": user}
    )
    for symbol, prev, close in (("ZGAP", "10.00", "11.00"), ("SPY", "100.00", "100.50")):
        for d, c in ((date(2098, 5, 5), prev), (session, close)):
            db_conn.execute(
                text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                     "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"),
                {"s": symbol, "d": d, "c": Decimal(c)},
            )

    store_emitted_claims(
        db_conn, user, f"{user}-{session}-open", session,
        [Claim("ZGAP", "premarket_gap", "up", 0)],
    )
    (resolved,) = resolve_due_claims(db_conn, user, session)
    assert resolved.symbol == "ZGAP"
    assert resolved.outcome == "correct"  # +10% vs SPY's +0.5%


def test_a_horizon_one_claim_is_not_resolved_on_its_own_session(db_conn: Connection) -> None:
    """The regression that matters: widening the due-claims query must not let
    the close brief grade the claim it emitted minutes earlier."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fd"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h1@example.invalid')"), {"u": user}
    )
    db_conn.execute(
        text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
             "VALUES ('ZH1', :d, 10, 10, 10, 10, 1000, 10)"),
        {"d": session},
    )
    store_emitted_claims(
        db_conn, user, "b", session, [Claim("ZH1", "relative_strength", "up", 1)]
    )
    assert resolve_due_claims(db_conn, user, session) == []
