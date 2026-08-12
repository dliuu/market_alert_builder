"""M7 against a real database (skipped without DATABASE_URL): position-risk flags
fire from synthetic fundamentals/events, and the correlation flag's weekly cap
holds across three consecutive close briefs — the DoD.

Far-future dates so seeding SPY / fundamentals can't collide with real backfilled
data. Each test runs inside a rolled-back transaction (conftest)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import assemble_and_store

_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fc"
_SECTOR = "ZZFLAG"


def _seed_user(conn: Connection) -> str:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-flags@example.invalid')"),
        {"u": _TEST_USER_ID},
    )
    return str(
        conn.execute(
            text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
            {"u": _TEST_USER_ID, "n": _SECTOR},
        ).scalar_one()
    )


def _seed_holding(conn: Connection, sector_id: str, symbol: str, opened_on: date) -> None:
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
        {"u": _TEST_USER_ID, "h": holding_id, "o": opened_on},
    )


def _seed_bar(conn: Connection, symbol: str, session_date: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": session_date, "c": Decimal(close)},
    )


def _flags_of(obj: Any) -> list[tuple[str, str | None, str]]:
    return [(f.type.value, f.symbol, f.text_key) for f in obj.flags]


# --- Position risk fires from synthetic fundamentals + events -------------


def test_position_risk_flags_fire(db_conn: Connection) -> None:
    sector_id = _seed_user(db_conn)
    prev, session = date(2099, 3, 2), date(2099, 3, 3)
    _seed_holding(db_conn, sector_id, "ZFA", prev)
    _seed_bar(db_conn, "ZFA", prev, "100")
    _seed_bar(db_conn, "ZFA", session, "110")  # +10% → a full-tier mover → brief sends

    # Runway 4q (< 6): cash 400, one burn 100. Dilution +20% (> 15%): 120 vs 100
    # shares a year earlier. Earnings in 3 days (≤ 7).
    db_conn.execute(
        text(
            "INSERT INTO fundamentals (symbol, as_of, cash_cents, quarterly_burn_cents, "
            "shares_out, next_earnings_date) VALUES "
            "('ZFA', :old, 400, 100, 100, NULL), "
            "('ZFA', :recent, 400, 100, 120, :earn)"
        ),
        {"old": date(2098, 2, 1), "recent": date(2099, 3, 1), "earn": session + timedelta(days=3)},
    )
    # A lockup (supply event) 5 days out (≤ 7).
    db_conn.execute(
        text("INSERT INTO events (symbol, event_type, occurs_at) VALUES ('ZFA', 'lockup', :d)"),
        {"d": session + timedelta(days=5)},
    )

    obj = assemble_and_store(db_conn, _TEST_USER_ID, session, "close")
    assert obj is not None
    fired = {t for t, _, _ in _flags_of(obj)}
    # Single-name concentration (100% of a one-name book), runway, dilution,
    # earnings_soon, supply_event all fire; sector concentration too.
    assert {"runway", "dilution", "earnings_soon", "supply_event", "concentration"} <= fired


# --- Correlation/concentration weekly cap across three consecutive briefs --


def test_weekly_rate_limit_holds_across_three_briefs(db_conn: Connection) -> None:
    sector_id = _seed_user(db_conn)
    start = date(2099, 6, 1)
    _seed_holding(db_conn, sector_id, "ZFB", start - timedelta(days=1))
    # A one-name book → 100% single-name (and sector) concentration on every
    # session. Each session the name moves > 1% so the close brief sends.
    prev_close = 100.0
    sessions = [start, start + timedelta(days=1), start + timedelta(days=2)]
    _seed_bar(db_conn, "ZFB", start - timedelta(days=1), f"{prev_close:.2f}")
    for d in sessions:
        prev_close *= 1.05  # +5% each session
        _seed_bar(db_conn, "ZFB", d, f"{prev_close:.2f}")

    def concentration_symbols(obj: Any) -> list[str | None]:
        return [f.symbol for f in obj.flags if f.type.value == "concentration"]

    briefs = [assemble_and_store(db_conn, _TEST_USER_ID, d, "close") for d in sessions]
    assert all(b is not None for b in briefs)

    # Mentioned in brief 1 only; the persistent condition is capped for 2 and 3.
    assert "ZFB" in concentration_symbols(briefs[0])
    assert concentration_symbols(briefs[1]) == []
    assert concentration_symbols(briefs[2]) == []

    # last_seen stayed pinned to the first mention (the rate-limit clock).
    last_seen = db_conn.execute(
        text(
            "SELECT last_seen FROM flags WHERE user_id = :u AND flag_type = 'concentration' "
            "AND symbol = 'ZFB'"
        ),
        {"u": _TEST_USER_ID},
    ).scalar_one()
    assert last_seen == sessions[0]

    # A fourth brief a full week later re-surfaces the flag.
    later = sessions[0] + timedelta(days=7)
    _seed_bar(db_conn, "ZFB", later - timedelta(days=1), "200")
    _seed_bar(db_conn, "ZFB", later, "220")  # +10%
    fourth = assemble_and_store(db_conn, _TEST_USER_ID, later, "close")
    assert fourth is not None
    assert "ZFB" in concentration_symbols(fourth)


def test_quiet_session_spends_no_rate_limit_budget(db_conn: Connection) -> None:
    # A brief that skips (no full-tier mover) must not record a flag mention.
    sector_id = _seed_user(db_conn)
    prev, session = date(2099, 9, 1), date(2099, 9, 2)
    _seed_holding(db_conn, sector_id, "ZFC", prev)
    _seed_bar(db_conn, "ZFC", prev, "100")
    _seed_bar(db_conn, "ZFC", session, "100.05")  # +0.05% → suppressed → brief skips

    obj = assemble_and_store(db_conn, _TEST_USER_ID, session, "close")
    assert obj is None

    rows = db_conn.execute(
        text("SELECT count(*) FROM flags WHERE user_id = :u"), {"u": _TEST_USER_ID}
    ).scalar_one()
    assert rows == 0  # nothing surfaced, nothing recorded
