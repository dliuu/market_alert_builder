"""Demonstrate M8 narration end to end, and its non-fatal guarantee.

Seeds a throwaway two-name book (both full-tier movers so the close brief
sends), then assembles the same brief three ways — inside a transaction that is
always rolled back:

  1. tables-only (no narrator): ``one_thing`` and every ``why`` are null;
  2. a stub narrator standing in for Claude: prose appears;
  3. a "revoked key" narrator that raises: the brief is byte-for-byte the
     tables-only object — proving revoking the key still ships a valid brief.

If ANTHROPIC_API_KEY is set, a fourth pass calls the real model.

Far-future dates so seeding can't collide with real backfilled bars.

Run as a module (from apps/worker):
    uv run python -m scripts.dry_run_narration
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import assemble_and_store
from worker.db import get_engine
from worker.narrate import default_narrator

_USER = "00000000-0000-0000-0000-0000000000f8"
_SECTOR = "ZZNARRDEMO"
_PREV = date(2099, 5, 1)
_SESSION = _PREV + timedelta(days=1)


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
        text("INSERT INTO users (id, email) VALUES (:u, 'narrdemo@example.invalid')"), {"u": _USER}
    )
    sector_id = conn.execute(
        text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
        {"u": _USER, "n": _SECTOR},
    ).scalar_one()
    for symbol, prev_c, c, shares, cost in (
        ("ZNA", "100", "110", "10", 9000),
        ("ZNB", "50", "44", "20", 4000),
    ):
        holding_id = conn.execute(
            text(
                "INSERT INTO holdings (user_id, sector_id, symbol) "
                "VALUES (:u, :s, :sym) RETURNING id"
            ),
            {"u": _USER, "s": sector_id, "sym": symbol},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
                "VALUES (:u, :h, :sh, :cost, :o)"
            ),
            {
                "u": _USER,
                "h": holding_id,
                "sh": shares,
                "cost": cost,
                "o": _PREV - timedelta(days=1),
            },
        )
        _bar(conn, symbol, _PREV, prev_c)
        _bar(conn, symbol, _SESSION, c)


def _stub_narrator(_prompt: str) -> str:
    return json.dumps(
        {
            "one_thing": "One name did the heavy lifting on an otherwise mixed session.",
            "why": {
                "ZNA": "Bid up on relative strength versus the tape.",
                "ZNB": "Drifted lower with no fresh catalyst.",
            },
        }
    )


def _revoked_narrator(_prompt: str) -> str:
    raise RuntimeError("401 Unauthorized — API key revoked")


def _show(label: str, conn: Connection, narrator) -> dict:  # type: ignore[no-untyped-def]
    # Each pass runs in its own savepoint so the M7 flag rate-limiter and claim
    # state don't carry between passes — every one sees the same fresh book.
    savepoint = conn.begin_nested()
    try:
        obj = assemble_and_store(conn, _USER, _SESSION, "close", narrator=narrator)
    finally:
        savepoint.rollback()
    assert obj is not None
    body = obj.model_dump(mode="json")
    whys = {r["symbol"]: r["why"] for r in body["sections"][0]["rows"]}
    print(f"\n=== {label} ===")
    print("  one_thing:", body["one_thing"])
    print("  why:", json.dumps(whys))
    return body


def main() -> None:
    conn = get_engine().connect()
    trans = conn.begin()
    try:
        _seed(conn)

        tables_only = _show("1. tables-only (no narrator)", conn, None)
        _show("2. stub narrator (Claude stand-in)", conn, _stub_narrator)
        revoked = _show("3. revoked key (narrator raises)", conn, _revoked_narrator)

        # Equal but for `generated_at` (a per-call clock): a revoked key changes
        # nothing about the brief's content — it just ships tables-only.
        tables_only.pop("generated_at")
        revoked.pop("generated_at")
        assert revoked == tables_only, "revoked-key brief must equal the tables-only brief"
        print("\n✓ revoked-key brief equals the tables-only brief — narration is non-fatal.")

        narrator = default_narrator()
        if narrator is not None:
            _show("4. live Claude (ANTHROPIC_API_KEY set)", conn, narrator)
        else:
            print("\n(ANTHROPIC_API_KEY unset — skipping the live Claude pass.)")
    finally:
        trans.rollback()
        conn.close()
        print("\nrolled back — no real rows changed.")


if __name__ == "__main__":
    main()
