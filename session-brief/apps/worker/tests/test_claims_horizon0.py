"""The morning claim (M15): the open brief commits to a direction before the
bell and the *same session's* close brief grades it.

This is the loop D16b built the engine for and could not exercise — until now
every claim was horizon 1, resolved by the next day's close brief. Horizon 0 is
a real change to resolution, not a free ride on the claim_type seam:
`resolve_due_claims` read only `session_date < :session_date`, and
`_resolve_session` offset by `horizon - 1`, which is -1 at horizon 0.
"""

from __future__ import annotations

from datetime import date, timedelta
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
    """DoD 4, end to end: emitted at 08:15, graded on that same session's
    open→close (I1, M15 review) — not close-to-close, which would fold the
    pre-market gap the claim is *about* back into the graded window. ZGAP opens
    post-gap and keeps rising into the close while SPY sits flat, so "up" is
    correct on the open→close measure alone."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fc"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h0@example.invalid')"), {"u": user}
    )
    for symbol, o, c in (("ZGAP", "10.50", "11.00"), ("SPY", "100.00", "100.00")):
        db_conn.execute(
            text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                 "VALUES (:s, :d, :o, :o, :o, :c, 1000, :c)"),
            {"s": symbol, "d": session, "o": Decimal(o), "c": Decimal(c)},
        )

    store_emitted_claims(
        db_conn, user, f"{user}-{session}-open", session,
        [Claim("ZGAP", "premarket_gap", "up", 0)],
        market="US",
    )
    (resolved,) = resolve_due_claims(db_conn, user, session, market="US", benchmark="SPY")
    assert resolved.symbol == "ZGAP"
    assert resolved.outcome == "correct"  # +4.8% open->close vs SPY's flat


def test_a_gap_up_that_fades_intraday_resolves_wrong(db_conn: Connection) -> None:
    """I1, M15 review: the defect this fixes. ZFADE's pre-market gap (implied,
    not represented here — the claim only carries direction) is a fact about
    the period *before* this bar; this bar's open already reflects the +4% gap.
    It then gives back ground into the close: -2.9% open→close while SPY sits
    flat, so the "up" claim resolves wrong even though close-to-close across the
    gap would have shown a net gain — which is exactly the bug (grading on a
    window that contains the interval the claim predicts)."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000ff"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h2@example.invalid')"), {"u": user}
    )
    for symbol, o, c in (("ZFADE", "104.00", "101.00"), ("SPY", "100.00", "100.00")):
        db_conn.execute(
            text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                 "VALUES (:s, :d, :o, :o, :o, :c, 1000, :c)"),
            {"s": symbol, "d": session, "o": Decimal(o), "c": Decimal(c)},
        )

    store_emitted_claims(
        db_conn, user, f"{user}-{session}-open", session,
        [Claim("ZFADE", "premarket_gap", "up", 0)],
        market="US",
    )
    (resolved,) = resolve_due_claims(db_conn, user, session, market="US", benchmark="SPY")
    assert resolved.symbol == "ZFADE"
    assert resolved.outcome == "wrong"


def test_a_horizon_one_claim_is_not_resolved_on_its_own_session(db_conn: Connection) -> None:
    """The regression that matters: widening the due-claims query must not let
    the close brief grade the claim it emitted minutes earlier.

    I5, M15 review: this must seed a D+1 bar (for ZH1 *and* SPY) so that the
    assertion actually exercises the `resolve_on > session_date` guard it's
    named for, rather than passing for the unrelated reason that
    `_resolve_session` finds no bar at all and `resolve_due_claims` bails out at
    the earlier `resolve_on is None` check. With only ZH1's D+1 bar seeded (no
    SPY), the outcome is still unresolved with the guard removed — `_grade`
    can't find a benchmark bar and returns `None` regardless — so that alone
    doesn't pin the guard either; SPY needs a real D and D+1 bar too, so
    `_grade` can actually produce an outcome once the guard no longer blocks it.

    Verified by temporarily replacing the guard with `if resolve_on is None:`
    (dropping the `> session_date` half): with this seeding, the claim got
    resolved (outcome "correct") instead of staying in the unresolved list —
    i.e. the test failed as expected. Restored the guard afterward.
    """
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fd"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h1@example.invalid')"), {"u": user}
    )
    # ZH1 rises 10% D→D+1; SPY is flat. If the guard didn't stop resolution at
    # `session`, `_grade` would have everything it needs to call this "correct".
    for symbol, c_d, c_d1 in (("ZH1", "10.00", "11.00"), ("SPY", "100.00", "100.00")):
        for d, c in ((session, c_d), (session + timedelta(days=1), c_d1)):
            db_conn.execute(
                text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                     "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"),
                {"s": symbol, "d": d, "c": Decimal(c)},
            )
    store_emitted_claims(
        db_conn, user, "b", session, [Claim("ZH1", "relative_strength", "up", 1)],
        market="US",
    )
    assert resolve_due_claims(db_conn, user, session, market="US", benchmark="SPY") == []


def test_a_horizon_zero_claim_waits_for_its_own_bar(db_conn: Connection) -> None:
    """Also-fix, M15 review: at 16:45 the close brief may run before session D's
    bar has landed. `_resolve_session` must return `None` (no bar yet) so the
    horizon-0 claim waits for the next run rather than being graded on data
    that doesn't exist."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-000000000100"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h3@example.invalid')"), {"u": user}
    )
    # No bars_daily row for ZWAIT at `session` at all — the bar hasn't landed.
    store_emitted_claims(
        db_conn, user, f"{user}-{session}-open", session,
        [Claim("ZWAIT", "premarket_gap", "up", 0)],
        market="US",
    )
    assert resolve_due_claims(db_conn, user, session, market="US", benchmark="SPY") == []
