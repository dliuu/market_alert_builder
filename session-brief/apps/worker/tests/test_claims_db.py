"""M6 against a real database (skipped without DATABASE_URL): a claim emitted by
one session's close brief is graded by a later session's, and the outcome
persists in `claims`. Far-future dates so seeding SPY can't collide with real
backfilled bars."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import assemble_and_store
from worker.constants import ATTRIBUTION_MODEL_VERSION

_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fb"
_P = date(2099, 1, 3)  # prev-of-emit
_E = date(2099, 1, 4)  # emit session
_D = date(2099, 1, 5)  # resolve session
_SECTOR = "ZZCLAIM"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-claims@example.invalid')"),
        {"u": _TEST_USER_ID},
    )


def _seed_bar(conn: Connection, symbol: str, session_date: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": session_date, "c": Decimal(close)},
    )


def _seed_lot(conn: Connection, symbol: str) -> None:
    sector_id = conn.execute(
        text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
        {"u": _TEST_USER_ID, "n": _SECTOR},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) "
            "VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": _TEST_USER_ID, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 9000, :o)"
        ),
        {"u": _TEST_USER_ID, "h": holding_id, "o": _P},
    )


def _seed(conn: Connection, zca_close_on_d: str, spy_close_on_d: str) -> None:
    """ZCA +5% on E (a full-tier mover) vs SPY +1% → an up relative_strength
    claim. The D closes decide whether the call was right."""
    _seed_user(conn)
    _seed_lot(conn, "ZCA")
    _seed_bar(conn, "ZCA", _P, "100")
    _seed_bar(conn, "ZCA", _E, "105")  # +5%
    _seed_bar(conn, "ZCA", _D, zca_close_on_d)
    _seed_bar(conn, "SPY", _P, "100")
    _seed_bar(conn, "SPY", _E, "101")  # +1%
    _seed_bar(conn, "SPY", _D, spy_close_on_d)


def _seed_attribution(conn: Connection, symbol: str, trade_date: date, resid_bps: str) -> None:
    """A minimal attribution row (market=theme=0 so total==resid) for grading:
    only resid_bps and its sign matter to `_grade`."""
    conn.execute(
        text(
            "INSERT INTO attribution (symbol, trade_date, model_version, market_bps, "
            "theme_bps, resid_bps, total_bps, beta_market, beta_theme, n_obs, computed_at) "
            "VALUES (:s, :d, :mv, 0, 0, :r, :r, 0, 0, 1, now())"
        ),
        {"s": symbol, "d": trade_date, "mv": ATTRIBUTION_MODEL_VERSION, "r": Decimal(resid_bps)},
    )


def _claim_row(conn: Connection, session_date: date = _E) -> dict[str, Any]:
    # Filter to the emit session: the resolve session's own close brief emits its
    # own claim, so there can be more than one row overall.
    row = conn.execute(
        text(
            "SELECT symbol, claim_type, direction, horizon_sessions, session_date, "
            "outcome, resolved_session, graded_model_version FROM claims "
            "WHERE user_id = :u AND session_date = :d"
        ),
        {"u": _TEST_USER_ID, "d": session_date},
    ).mappings().one()
    return dict(row)


def test_emit_then_resolve_correct(db_conn: Connection) -> None:
    # Grading is against the realized residual sign (M13 §3), not the raw
    # sym-vs-SPY return: a positive resid_bps on D makes the up call right.
    _seed(db_conn, zca_close_on_d="110", spy_close_on_d="101.5")
    _seed_attribution(db_conn, "ZCA", _D, resid_bps="50")

    emit = assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")
    assert emit is not None
    # The emitted claim is carried in the object with a null outcome.
    assert [(c.symbol, c.type.value, c.direction.value) for c in emit.claims] == [
        ("ZCA", "relative_strength", "up")
    ]
    assert emit.resolved_claims == []
    assert _claim_row(db_conn)["outcome"] is None  # persisted, unresolved

    resolve = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")
    assert resolve is not None
    assert [(c.symbol, c.outcome.value) for c in resolve.resolved_claims] == [("ZCA", "correct")]

    row = _claim_row(db_conn)
    assert row["outcome"] == "correct"  # outcome persists (Done)
    assert row["resolved_session"] == _D
    assert row["graded_model_version"] == ATTRIBUTION_MODEL_VERSION


def test_emit_then_resolve_wrong(db_conn: Connection) -> None:
    # On D, ZCA +1.9% is still a full-tier mover (so the brief sends). A
    # negative resid_bps makes the up call wrong.
    _seed(db_conn, zca_close_on_d="107", spy_close_on_d="103")
    _seed_attribution(db_conn, "ZCA", _D, resid_bps="-50")

    assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")
    resolve = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")

    assert resolve is not None
    assert [(c.symbol, c.outcome.value) for c in resolve.resolved_claims] == [("ZCA", "wrong")]
    row = _claim_row(db_conn)
    assert row["outcome"] == "wrong"
    assert row["graded_model_version"] == ATTRIBUTION_MODEL_VERSION


def test_resolution_is_idempotent(db_conn: Connection) -> None:
    _seed(db_conn, zca_close_on_d="110", spy_close_on_d="101.5")
    _seed_attribution(db_conn, "ZCA", _D, resid_bps="50")
    assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")

    first = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")
    assert first is not None and len(first.resolved_claims) == 1
    # Re-running D must not re-grade the already-resolved claim.
    second = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")
    assert second is not None and second.resolved_claims == []
    assert _claim_row(db_conn)["outcome"] == "correct"


def test_beta_doesnt_earn_credit_raw_right_residual_wrong(db_conn: Connection) -> None:
    # D17/DoD#2: a name can beat SPY in raw terms purely on market beta while
    # its idiosyncratic move (resid_bps) is actually negative. Raw ZCA (+4.76%)
    # beats raw SPY (+0.5%) on D — an old-style grader would call the up claim
    # "correct" — but the realized residual is negative, so it must be "wrong".
    _seed(db_conn, zca_close_on_d="110", spy_close_on_d="101.5")
    _seed_attribution(db_conn, "ZCA", _D, resid_bps="-25")

    assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")
    resolve = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")

    assert resolve is not None
    assert [(c.symbol, c.outcome.value) for c in resolve.resolved_claims] == [("ZCA", "wrong")]
    row = _claim_row(db_conn)
    assert row["outcome"] == "wrong"
    assert row["graded_model_version"] == ATTRIBUTION_MODEL_VERSION


def test_beta_doesnt_earn_credit_mirror_raw_wrong_residual_right(db_conn: Connection) -> None:
    # Mirror of the above: raw ZCA (+1.9%) trails raw SPY (+2.0%) on D — an
    # old-style grader would call the up claim "wrong" — but the realized
    # residual is positive, so it must be "correct".
    _seed(db_conn, zca_close_on_d="107", spy_close_on_d="103")
    _seed_attribution(db_conn, "ZCA", _D, resid_bps="25")

    assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")
    resolve = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")

    assert resolve is not None
    assert [(c.symbol, c.outcome.value) for c in resolve.resolved_claims] == [("ZCA", "correct")]
    row = _claim_row(db_conn)
    assert row["outcome"] == "correct"
    assert row["graded_model_version"] == ATTRIBUTION_MODEL_VERSION


def test_no_attribution_row_stays_unresolved(db_conn: Connection) -> None:
    # No attribution row for (ZCA, D, model_version) — can't grade, so the
    # claim stays unresolved rather than falling back to raw bars (DoD#2's
    # graceful "missing data" path, mirroring the old missing-benchmark-bar one).
    _seed(db_conn, zca_close_on_d="110", spy_close_on_d="101.5")

    assemble_and_store(db_conn, _TEST_USER_ID, _E, "close")
    resolve = assemble_and_store(db_conn, _TEST_USER_ID, _D, "close")

    assert resolve is not None
    assert resolve.resolved_claims == []
    row = _claim_row(db_conn)
    assert row["outcome"] is None
    assert row["resolved_session"] is None
    assert row["graded_model_version"] is None
