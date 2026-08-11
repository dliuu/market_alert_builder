"""Worker CLI entrypoint."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker.compute import compute_and_store
from worker.constants import DEV_USER_ID
from worker.db import get_engine
from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars
from worker.providers.tiingo import TiingoProvider


def main() -> None:
    parser = argparse.ArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("hello", help="smoke check that the worker boots")

    backfill = sub.add_parser("backfill", help="ingest + normalize daily bars")
    backfill.add_argument(
        "--symbols", help="comma-separated tickers; defaults to the symbols in your book"
    )
    backfill.add_argument("--days", type=int, default=90, help="look-back window (default 90)")

    compute = sub.add_parser("compute", help="compute returns/P&L/contribution for a session")
    compute.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest bar")
    compute.add_argument("--user", default=DEV_USER_ID, help="user id (defaults to the dev user)")

    args = parser.parse_args()

    if args.command == "backfill":
        _backfill(symbols_arg=args.symbols, days=args.days)
        return

    if args.command == "compute":
        _compute(date_arg=args.date, user_id=args.user)
        return

    if args.command in (None, "hello"):
        print("worker: ok")


def _backfill(symbols_arg: str | None, days: int) -> None:
    engine = get_engine()
    symbols = _resolve_symbols(symbols_arg, engine)
    if not symbols:
        raise SystemExit(
            "No symbols to backfill. Pass --symbols AAPL,MSFT or add holdings to your book."
        )

    end = date.today()
    start = end - timedelta(days=days)
    provider = TiingoProvider()

    with engine.begin() as conn:
        written = ingest_daily_bars(conn, provider, symbols, start, end)
        bars = normalize_bars(conn, symbols)

    print(
        f"backfill: {len(symbols)} symbol(s) {start}..{end} — "
        f"{written} new payload(s), {bars} bar(s) normalized"
    )


def _resolve_symbols(symbols_arg: str | None, engine: Engine) -> list[str]:
    if symbols_arg:
        return [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT symbol FROM holdings ORDER BY symbol")).all()
    return [str(row[0]) for row in rows]


def _compute(date_arg: str | None, user_id: str) -> None:
    engine = get_engine()
    session_date = date.fromisoformat(date_arg) if date_arg else _latest_session(engine)
    if session_date is None:
        raise SystemExit("No bars found. Run `backfill` first.")

    with engine.begin() as conn:
        result = compute_and_store(conn, user_id, session_date)

    book = result.book
    if not result.positions:
        print(f"compute {session_date}: no open positions for this user.")
        return

    print(
        f"compute {session_date}: value {_dollars(book.value_cents)}, "
        f"day {_dollars(book.day_pnl_cents, signed=True)} ({_bps(book.day_bps)}), "
        f"total {_dollars(book.total_pnl_cents, signed=True)} ({_pct(book.total_pct)}), "
        f"vs SPY {_bps(book.vs_spy_bps)}"
    )
    for position in result.positions:
        print(
            f"  {position.symbol:6} day {_dollars(position.day_pnl_cents, signed=True)} "
            f"contrib {_bps(position.contribution_bps)}  weight {_pct(position.weight)}"
        )

    # The invariant, checked live: the parts must sum to the whole.
    total_contrib = sum(
        (p.contribution_bps for p in result.positions if p.contribution_bps is not None),
        Fraction(0),
    )
    identity = book.day_bps is not None and total_contrib == book.day_bps
    print(f"  Σ contribution_bps = {_bps(total_contrib)}  vs  day_bps = {_bps(book.day_bps)}  "
          f"[{'OK' if identity else 'n/a — book opened today'}]")


def _latest_session(engine: Engine) -> date | None:
    with engine.connect() as conn:
        value = conn.execute(text("SELECT max(session_date) FROM bars_daily")).scalar()
    return value


def _dollars(cents: int, *, signed: bool = False) -> str:
    sign = "+" if signed and cents >= 0 else ""
    return f"{sign}${cents / 100:,.2f}"


def _bps(value: Fraction | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.1f}bps"


def _pct(value: Fraction | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:+.2f}%"


if __name__ == "__main__":
    main()
