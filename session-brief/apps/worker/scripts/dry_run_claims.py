"""Demonstrate the M6 accountability loop end to end, without touching the real
book. Seeds two sessions for a throwaway name (which outperforms the benchmark on
the emit session, earning an `up` relative_strength claim), assembles both close
briefs, and shows the claim emitted then graded — all inside a transaction that
is always rolled back.

Far-future dates so seeding SPY can't collide with real backfilled bars.

Run as a module (from apps/worker):
    uv run python -m scripts.dry_run_claims          # the call proves right → correct
    uv run python -m scripts.dry_run_claims --miss   # the call proves wrong  → wrong
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import text

from worker.assemble import assemble_and_store
from worker.db import get_engine

_USER = "00000000-0000-0000-0000-0000000000fa"
_SECTOR = "ZZCLAIMDEMO"
_P = date(2099, 1, 3)
_E = date(2099, 1, 4)  # emit session
_D = date(2099, 1, 5)  # resolve session


def _bar(conn: object, symbol: str, d: date, close: str) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": d, "c": close},
    )


def _seed(conn: object, miss: bool) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO users (id, email) VALUES (:u, 'claimdemo@example.invalid')"), {"u": _USER}
    )
    sector_id = conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
        {"u": _USER, "n": _SECTOR},
    ).scalar_one()
    holding_id = conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) "
            "VALUES (:u, :sec, 'ZCA') RETURNING id"
        ),
        {"u": _USER, "sec": sector_id},
    ).scalar_one()
    conn.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 9000, :o)"
        ),
        {"u": _USER, "h": holding_id, "o": _P},
    )
    # ZCA +5% on E (a full-tier mover) vs SPY +1% → an `up` claim.
    _bar(conn, "ZCA", _P, "100")
    _bar(conn, "ZCA", _E, "105")
    _bar(conn, "SPY", _P, "100")
    _bar(conn, "SPY", _E, "101")
    # On D: correct → ZCA (+4.76%) beats SPY (+0.5%); miss → ZCA (+1.9%) lags SPY (+2.0%).
    _bar(conn, "ZCA", _D, "107" if miss else "110")
    _bar(conn, "SPY", _D, "103" if miss else "101.5")


def _print_claims(obj: object, label: str) -> None:
    body = obj.model_dump(mode="json")  # type: ignore[attr-defined]
    print(f"\n=== {label} ({body['session_date']}) ===")
    print("  claims (emitted):    ", json.dumps(body["claims"]))
    print("  resolved_claims:     ", json.dumps(body["resolved_claims"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--miss", action="store_true", help="make the call resolve wrong")
    args = parser.parse_args()

    conn = get_engine().connect()
    trans = conn.begin()
    try:
        _seed(conn, args.miss)
        emit = assemble_and_store(conn, _USER, _E, "close")
        resolve = assemble_and_store(conn, _USER, _D, "close")
        if emit is not None:
            _print_claims(emit, "EMIT session")
        if resolve is not None:
            _print_claims(resolve, "RESOLVE session")

        print("\n=== claims table (persisted) ===")
        for r in conn.execute(
            text(
                "SELECT symbol, direction, session_date, outcome, resolved_session "
                "FROM claims WHERE user_id = :u ORDER BY session_date"
            ),
            {"u": _USER},
        ).mappings():
            print(f"  {dict(r)}")
    finally:
        trans.rollback()
        conn.close()
        print("\nrolled back — no real rows changed.")


if __name__ == "__main__":
    main()
