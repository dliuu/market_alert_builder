"""Demonstrate the M7 flags engine end to end, without touching the real book.

Seeds a throwaway one-name book (so the name is 100% of the book → single-name +
sector concentration) with synthetic fundamentals (low runway, high dilution,
earnings soon) and a lockup event, then assembles three consecutive close briefs.
Shows the position-risk flags firing and the correlation/concentration flag being
mentioned in the first brief only — the once-a-week cap (docs/05) — all inside a
transaction that is always rolled back.

Far-future dates so seeding can't collide with real backfilled bars.

Run as a module (from apps/worker):
    uv run python -m scripts.dry_run_flags
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import assemble_and_store
from worker.db import get_engine

_USER = "00000000-0000-0000-0000-0000000000f9"
_SECTOR = "ZZFLAGDEMO"
_START = date(2099, 4, 1)
_SESSIONS = [_START, _START + timedelta(days=1), _START + timedelta(days=2)]


def _bar(conn: Connection, symbol: str, d: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": d, "c": close},
    )


def _seed(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'flagdemo@example.invalid')"), {"u": _USER}
    )
    sector_id = conn.execute(
        text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
        {"u": _USER, "n": _SECTOR},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) "
            "VALUES (:u, :s, 'ZFD') RETURNING id"
        ),
        {"u": _USER, "s": sector_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 9000, :o)"
        ),
        {"u": _USER, "h": holding_id, "o": _START - timedelta(days=1)},
    )

    # +5% every session → a full-tier mover each day, so every close brief sends.
    close = 100.0
    _bar(conn, "ZFD", _START - timedelta(days=1), f"{close:.2f}")
    for d in _SESSIONS:
        close *= 1.05
        _bar(conn, "ZFD", d, f"{close:.2f}")

    # Position risk: runway 4q (< 6), dilution +20% (> 15%), earnings in 3 days.
    conn.execute(
        text(
            "INSERT INTO fundamentals (symbol, as_of, cash_cents, quarterly_burn_cents, "
            "shares_out, next_earnings_date) VALUES "
            "('ZFD', :old, 400, 100, 100, NULL), ('ZFD', :recent, 400, 100, 120, :earn)"
        ),
        {"old": date(2098, 3, 1), "recent": _START - timedelta(days=1),
         "earn": _START + timedelta(days=3)},
    )
    conn.execute(
        text("INSERT INTO events (symbol, event_type, occurs_at) VALUES ('ZFD', 'lockup', :d)"),
        {"d": _START + timedelta(days=5)},
    )


def main() -> None:
    conn = get_engine().connect()
    trans = conn.begin()
    try:
        _seed(conn)
        for i, d in enumerate(_SESSIONS, start=1):
            obj = assemble_and_store(conn, _USER, d, "close")
            print(f"\n=== brief {i} ({d}) ===")
            if obj is None:
                print("  (skipped — no full-tier mover)")
                continue
            print("  flags:", json.dumps([f.model_dump(mode="json") for f in obj.flags], indent=2))

        print("\n=== flags table (persisted) ===")
        for r in conn.execute(
            text(
                "SELECT flag_type, symbol, first_seen, last_seen FROM flags "
                "WHERE user_id = :u ORDER BY flag_type, symbol"
            ),
            {"u": _USER},
        ).mappings():
            print(f"  {dict(r)}")
        print("\nNote: concentration/correlation appear in brief 1 only (weekly cap);")
        print("position-risk flags appear every send.")
    finally:
        trans.rollback()
        conn.close()
        print("\nrolled back — no real rows changed.")


if __name__ == "__main__":
    main()
