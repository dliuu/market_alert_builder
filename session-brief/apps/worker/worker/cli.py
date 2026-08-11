"""Worker CLI entrypoint."""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

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

    args = parser.parse_args()

    if args.command == "backfill":
        _backfill(symbols_arg=args.symbols, days=args.days)
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


if __name__ == "__main__":
    main()
