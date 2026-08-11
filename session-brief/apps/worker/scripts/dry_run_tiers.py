"""Demonstrate M5 suppression + tape quality on synthetic throwaway symbols,
without touching the real book. Seeds bars for three names chosen to land one in
each tier (full / brief / suppressed), assembles the close brief, and prints it —
all inside a transaction that is always rolled back, so nothing persists.

Run as a module (from apps/worker):
    uv run python -m scripts.dry_run_tiers          # mixed session (sends)
    uv run python -m scripts.dry_run_tiers --quiet  # nothing >1% (skipped)
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from sqlalchemy import text

from worker.assemble import assemble_and_store, close_brief_should_skip
from worker.compute import compute_and_store
from worker.db import get_engine

# A throwaway tenant + junk tickers, distinct from any real book.
_USER = "00000000-0000-0000-0000-0000000000fc"
_SECTOR = "ZZTIER"
_PREV = date(2026, 8, 10)
_SESSION = date(2026, 8, 11)

# (symbol, prev_close, close) — moves chosen to hit each tier.
_MIXED = [("ZTFULL", "100", "112"), ("ZTBRIEF", "100", "99.5"), ("ZTSUPP", "100", "100.1")]
_QUIET = [("ZTBRIEF", "100", "99.5"), ("ZTSUPP", "100", "100.1")]  # no full-tier mover


def _seed(conn: object, names: list[tuple[str, str, str]]) -> None:
    conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO users (id, email) VALUES (:u, 'tiers@example.invalid')"), {"u": _USER}
    )
    sector_id = conn.execute(  # type: ignore[attr-defined]
        text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
        {"u": _USER, "n": _SECTOR},
    ).scalar_one()
    for symbol, prev_c, c in names:
        # 30 prior flat-volume bars so RVOL is defined; session volume is flat too
        # (rvol ~1x) so tiers here are driven purely by price move. The RVOL-spike
        # promotion path is covered by the unit tests.
        day = _SESSION - timedelta(days=45)
        for _ in range(30):
            conn.execute(  # type: ignore[attr-defined]
                text(
                    "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                    "VALUES (:s, :d, :p, :p, :p, :p, 1000, :p) ON CONFLICT DO NOTHING"
                ),
                {"s": symbol, "d": day, "p": prev_c},
            )
            day += timedelta(days=1)
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES (:s, :d, :p, :p, :p, :p, 1000, :p)"
            ),
            {"s": symbol, "d": _PREV, "p": prev_c},
        )
        # session bar: real intraday range, flat volume (rvol ~1x)
        hi = str(max(float(prev_c), float(c)) + 1)
        lo = str(min(float(prev_c), float(c)) - 1)
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES (:s, :d, :o, :h, :l, :c, 1000, :c)"
            ),
            {"s": symbol, "d": _SESSION, "o": prev_c, "h": hi, "l": lo, "c": c},
        )
        holding_id = conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO holdings (user_id, sector_id, symbol) "
                "VALUES (:u, :sec, :s) RETURNING id"
            ),
            {"u": _USER, "sec": sector_id, "s": symbol},
        ).scalar_one()
        conn.execute(  # type: ignore[attr-defined]
            text(
                "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
                "VALUES (:u, :h, 100, 9000, :o)"
            ),
            {"u": _USER, "h": holding_id, "o": _PREV},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet", action="store_true", help="seed a session with no full-tier mover"
    )
    args = parser.parse_args()

    conn = get_engine().connect()
    trans = conn.begin()
    try:
        _seed(conn, _QUIET if args.quiet else _MIXED)
        obj = assemble_and_store(conn, _USER, _SESSION, "close")
        if obj is None:
            # Rebuild once (ignoring the skip) so we can still show the shape.
            result = compute_and_store(conn, _USER, _SESSION)
            names = [p.symbol for p in result.positions]
            print(f"close brief SKIPPED — nothing moved >1%. (held: {', '.join(names)})")
        else:
            print(json.dumps(obj.model_dump(mode="json"), indent=2))
            print(f"\nskip check: close_brief_should_skip = {close_brief_should_skip(obj)}")
    finally:
        trans.rollback()
        conn.close()
        print("\nrolled back — no real rows changed.")


if __name__ == "__main__":
    main()
