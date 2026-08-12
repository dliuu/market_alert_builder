"""Worker CLI entrypoint."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker.assemble import assemble_and_store
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

    brief = sub.add_parser("brief", help="assemble a BriefObject for a session")
    brief.add_argument("--kind", default="close", choices=("open", "close"), help="brief kind")
    brief.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest bar")
    brief.add_argument("--user", default=DEV_USER_ID, help="user id (defaults to the dev user)")
    brief.add_argument(
        "--dry-run", action="store_true", help="print the object; do not write the briefs row"
    )
    brief.add_argument(
        "--no-narrate",
        action="store_true",
        help="skip Claude narration; ship the brief tables-only",
    )

    schedule = sub.add_parser(
        "schedule", help="run the unattended daily scheduler + dead-man's switch (M10)"
    )
    schedule.add_argument(
        "--once",
        action="store_true",
        help="run today's go/no-go job once and exit (no blocking loop)",
    )
    schedule.add_argument(
        "--dry-run",
        action="store_true",
        help="print the next few fire times and exit; nothing runs or sends",
    )

    send = sub.add_parser("send", help="render a stored brief and email it via Resend")
    send.add_argument("--kind", default="close", choices=("open", "close"), help="brief kind")
    send.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest bar")
    send.add_argument("--user", default=DEV_USER_ID, help="user id (defaults to the dev user)")
    send.add_argument("--to", help="recipient; defaults to BRIEF_RECIPIENT in .env")
    send.add_argument(
        "--dry-run",
        action="store_true",
        help="render and report size; do not touch Resend or the deliveries table",
    )

    args = parser.parse_args()

    if args.command == "backfill":
        _backfill(symbols_arg=args.symbols, days=args.days)
        return

    if args.command == "compute":
        _compute(date_arg=args.date, user_id=args.user)
        return

    if args.command == "brief":
        _brief(
            kind=args.kind,
            date_arg=args.date,
            user_id=args.user,
            dry_run=args.dry_run,
            narrate=not args.no_narrate,
        )
        return

    if args.command == "schedule":
        _schedule(once=args.once, dry_run=args.dry_run)
        return

    if args.command == "send":
        _send(
            kind=args.kind,
            date_arg=args.date,
            user_id=args.user,
            to=args.to,
            dry_run=args.dry_run,
        )
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


def _brief(
    kind: str, date_arg: str | None, user_id: str, dry_run: bool, narrate: bool
) -> None:
    import json

    from worker.narrate import default_narrator

    engine = get_engine()
    session_date = date.fromisoformat(date_arg) if date_arg else _latest_session(engine)
    if session_date is None:
        raise SystemExit("No bars found. Run `backfill` first.")

    # `default_narrator()` is None when ANTHROPIC_API_KEY is unset, so a keyless
    # run degrades to tables-only rather than failing (M8).
    narrator = default_narrator() if narrate else None

    conn = engine.connect()
    trans = conn.begin()
    try:
        obj = assemble_and_store(conn, user_id, session_date, kind, narrator=narrator)
        if dry_run:
            trans.rollback()  # --dry-run assembles but writes nothing
        else:
            trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()

    if obj is None:
        print(f"brief: {kind} {session_date} skipped — nothing moved >1%.")
        return

    print(json.dumps(obj.model_dump(mode="json"), indent=2))
    verb = "would write (dry-run)" if dry_run else "wrote"
    print(f"brief: {verb} {obj.brief_id}")


_MAX_HTML_BYTES = 80 * 1024  # Gmail clips near 102KB; the close brief stays under 80 (docs/06).


def _send(kind: str, date_arg: str | None, user_id: str, to: str | None, dry_run: bool) -> None:
    from worker import config
    from worker.deliver import deliver_brief

    recipient = to or config.BRIEF_RECIPIENT
    if not recipient:
        raise SystemExit("No recipient. Pass --to you@example.com or set BRIEF_RECIPIENT in .env.")
    if not dry_run:
        if not config.RESEND_API_KEY:
            raise SystemExit("RESEND_API_KEY is not set. Add it to .env (see docs/06).")
        if not config.BRIEF_FROM:
            raise SystemExit("BRIEF_FROM is not set. Add it to .env — see docs/06.")

    engine = get_engine()
    session_date = date.fromisoformat(date_arg) if date_arg else _latest_session(engine)
    if session_date is None:
        raise SystemExit("No bars found. Run `backfill` first.")

    conn = engine.connect()
    trans = conn.begin()
    try:
        result = deliver_brief(
            conn,
            user_id=user_id,
            session_date=session_date,
            kind=kind,
            recipient=recipient,
            sender=config.BRIEF_FROM,
            dry_run=dry_run,
        )
        if dry_run:
            trans.rollback()  # --dry-run writes no deliveries row
        else:
            trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()

    if result.html_bytes is not None:
        over = result.html_bytes >= _MAX_HTML_BYTES
        flag = "  OVER 80KB — will clip in Gmail" if over else ""
        print(f"send: html {result.html_bytes:,} bytes ({result.html_bytes / 1024:.1f} KB){flag}")
    if result.detail:
        print(f"send: {result.detail}")
    if result.status == "dry_run":
        print(f"send: dry-run for {kind} {session_date} → {recipient} (nothing sent)")
    elif result.status == "skipped":
        print(f"send: {kind} {session_date} → {recipient} already sent; skipped")
    else:
        print(f"send: {kind} {session_date} → {recipient} sent (msg {result.provider_msg_id})")


def _schedule(once: bool, dry_run: bool) -> None:
    from datetime import timedelta

    from worker import config
    from worker.scheduler import next_fire, run_scheduler, run_session_job

    if dry_run:
        from datetime import UTC, datetime

        delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
        now = datetime.now(UTC)
        print(f"schedule: now {now.isoformat()}, send delay {config.SEND_DELAY_MINUTES}min")
        for _ in range(5):
            now = next_fire(now, delay)
            print(f"  next fire: {now.isoformat()}")
            now = now + timedelta(seconds=1)
        return

    engine = get_engine()
    if once:
        from datetime import UTC, datetime

        outcome = run_session_job(engine, now_utc=datetime.now(UTC))
        print(f"schedule --once: {outcome}")
        return

    run_scheduler(engine)


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
