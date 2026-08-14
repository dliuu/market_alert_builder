"""Worker CLI entrypoint."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from contracts.brief import BriefObject
from worker import calendar
from worker.assemble import assemble_and_store
from worker.assemble_open import assemble_open_and_store
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

    events_seed = sub.add_parser(
        "events-seed", help="seed the synthetic §4 calendar (earnings/ex-div/lockup/macro)"
    )
    events_seed.add_argument("--date", help="session date YYYY-MM-DD; defaults to today")
    events_seed.add_argument(
        "--symbols", help="comma-separated tickers; defaults to the symbols in your book"
    )

    seed_premarket = sub.add_parser(
        "seed-premarket",
        help="capture a session's pre-market quotes + macro tape (M15 bootstrap)",
    )
    seed_premarket.add_argument("--date", help="session date YYYY-MM-DD; defaults to today")

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
        help="print the next few fire times (both kinds) and exit; nothing runs or sends",
    )
    schedule.add_argument(
        "--kind",
        default="close",
        choices=("open", "close"),
        help="which job --once runs (default close); ignored by the blocking loop",
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

    attribution = sub.add_parser("attribution", help="return attribution stages (M11)")
    attr_sub = attribution.add_subparsers(dest="attr_command")
    attr_sub.add_parser("themes-seed", help="seed reference themes + PIT members")
    a_refit = attr_sub.add_parser("refit", help="weekly: fit beta over trailing 120 sessions")
    a_refit.add_argument("--date", required=True, help="fit date YYYY-MM-DD")
    a_score = attr_sub.add_parser("score", help="decompose today's move from the current fit")
    a_score.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    a_score.add_argument(
        "--pm", action="store_true", help="score from synthetic PM bars (provisional)"
    )
    a_recon = attr_sub.add_parser("reconcile", help="reconcile synthetic vs official; mark revised")
    a_recon.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    a_recon.add_argument("--am", action="store_true", help="AM run against the official bar")

    a_signals = attr_sub.add_parser("signals", help="weekly derived signals + theme dispersion")
    a_signals.add_argument("--date", required=True, help="trade date YYYY-MM-DD")

    a_maintenance = attr_sub.add_parser(
        "maintenance", help="dashboard-only theme_misfit + beta_instability flags (M13 Task 7)"
    )
    a_maintenance.add_argument("--date", required=True, help="session date YYYY-MM-DD")

    a_concordance = attr_sub.add_parser(
        "concordance", help="reportable check: residual salience vs event dates (M13 Task 8)"
    )
    a_concordance.add_argument("--start", required=True, help="range start YYYY-MM-DD")
    a_concordance.add_argument("--end", required=True, help="range end YYYY-MM-DD")

    catalysts = sub.add_parser("catalysts", help="insider flow + Form 144 catalysts (M17)")
    cat_sub = catalysts.add_subparsers(dest="cat_command")
    c_ingest = cat_sub.add_parser("ingest", help="pull filings into raw_payloads + typed tables")
    c_ingest.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    c_ingest.add_argument("--symbols", help="comma-separated; defaults to the book")
    c_detect = cat_sub.add_parser(
        "detect", help="recompute signals from stored rows (no API calls)"
    )
    c_detect.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    c_rebuild = cat_sub.add_parser(
        "rebuild", help="drop and rebuild signals; reporting state is preserved"
    )
    c_rebuild.add_argument("--date", required=True, help="session date YYYY-MM-DD")

    args = parser.parse_args()

    if args.command == "catalysts":
        _catalysts(args.cat_command, date_arg=getattr(args, "date", None),
                   symbols_arg=getattr(args, "symbols", None))
        return

    if args.command == "backfill":
        _backfill(symbols_arg=args.symbols, days=args.days)
        return

    if args.command == "compute":
        _compute(date_arg=args.date, user_id=args.user)
        return

    if args.command == "events-seed":
        _events_seed(date_arg=args.date, symbols_arg=args.symbols)
        return

    if args.command == "seed-premarket":
        _seed_premarket(date_arg=args.date)
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
        _schedule(once=args.once, dry_run=args.dry_run, kind=args.kind)
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

    if args.command == "attribution":
        _attribution(args.attr_command, date_arg=getattr(args, "date", None),
                     pm=getattr(args, "pm", False),
                     start_arg=getattr(args, "start", None), end_arg=getattr(args, "end", None))
        return

    if args.command in (None, "hello"):
        print("worker: ok")


def _catalysts(
    cat_command: str | None, *, date_arg: str | None, symbols_arg: str | None
) -> None:
    """M17 stages. ``ingest`` is the only one that touches a vendor; ``detect``
    and ``rebuild`` read the stored rows alone, which is what makes a rule
    change cost zero API calls."""
    from worker.catalysts import rebuild_signals
    from worker.catalysts_ingest import ingest_catalysts
    from worker.constants import CATALYST_MODEL_VERSION, DEV_USER_ID
    from worker.providers.synthetic import SyntheticCatalystProvider

    if date_arg is None:
        raise SystemExit("--date is required")
    session_date = date.fromisoformat(date_arg)
    engine = get_engine()

    with engine.begin() as conn:
        if cat_command == "ingest":
            symbols = (
                [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
                if symbols_arg else _book_symbols(conn, DEV_USER_ID)
            )
            # The live FdnProvider swaps in here once M16's FdnClient lands;
            # nothing above the seam changes (D29).
            counts = ingest_catalysts(
                conn, SyntheticCatalystProvider(session_date), symbols, as_of=session_date
            )
            print(f"catalysts ingest {session_date}: {counts}")
            return

        if cat_command in ("detect", "rebuild"):
            floats = _book_floats(conn, session_date)
            stored = rebuild_signals(
                conn, model_version=CATALYST_MODEL_VERSION,
                as_of=session_date, earnings=_next_earnings(conn), floats=floats,
            )
            print(f"catalysts {cat_command} {session_date}: {stored} signals")
            return

    raise SystemExit("usage: catalysts {ingest|detect|rebuild} --date YYYY-MM-DD")


def _book_symbols(conn: Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        text("SELECT DISTINCT symbol FROM holdings WHERE user_id = :u ORDER BY symbol"),
        {"u": user_id},
    ).scalars().all()
    return [str(s) for s in rows]


def _next_earnings(conn: Connection) -> dict[str, date]:
    """The earnings dates `pre_earnings` measures against, from the `events`
    table M14 populates. A symbol absent here simply skips the rule."""
    rows = conn.execute(text(
        "SELECT symbol, min(occurs_at::date) AS d FROM events "
        "WHERE event_type = 'earnings' AND symbol IS NOT NULL "
        "AND occurs_at >= now() GROUP BY symbol"
    )).mappings().all()
    return {r["symbol"]: r["d"] for r in rows}


def _book_floats(conn: Connection, session_date: date) -> dict[str, Decimal]:
    """Public float per symbol, from `fundamentals.shares_out` where it exists.
    Shares outstanding overstates float, so this is an upper bound and
    `large_144` is correspondingly conservative — open question 4 is whether the
    vendor exposes a true float."""
    rows = conn.execute(text(
        "SELECT DISTINCT ON (symbol) symbol, shares_out FROM fundamentals "
        "WHERE shares_out IS NOT NULL AND as_of <= :d ORDER BY symbol, as_of DESC"
    ), {"d": session_date}).mappings().all()
    return {r["symbol"]: Decimal(str(r["shares_out"])) for r in rows}


def _attribution(
    attr_command: str | None, *, date_arg: str | None, pm: bool,
    start_arg: str | None = None, end_arg: str | None = None,
) -> None:
    from datetime import UTC, datetime

    from worker.constants import ATTRIBUTION_MODEL_VERSION

    engine = get_engine()
    now = datetime.now(UTC)

    if attr_command == "concordance":
        from worker.concordance import report_concordance

        if start_arg is None or end_arg is None:
            raise SystemExit("--start and --end are required")
        start = date.fromisoformat(start_arg)
        end = date.fromisoformat(end_arg)
        with engine.connect() as conn:
            report = report_concordance(conn, start, end, model_version=ATTRIBUTION_MODEL_VERSION)
        print(
            f"concordance {start}..{end}: observed {report.observed_rate:.3f} vs "
            f"baseline {report.baseline_rate:.3f} (lift {report.lift:.2f}x), "
            f"n_high={report.n_high} n_events={report.n_events}"
        )
        return

    if attr_command == "themes-seed":
        from worker.themes_seed import seed_themes

        with engine.begin() as conn:
            n = seed_themes(conn)
        print(f"seeded {n} new theme members")
        return

    if date_arg is None:
        raise SystemExit("--date is required")
    trade_date = date.fromisoformat(date_arg)

    if attr_command == "refit":
        from worker.attribution import refit

        with engine.begin() as conn:
            refit_result = refit(conn, trade_date, now_utc=now,
                                 model_version=ATTRIBUTION_MODEL_VERSION)
        print(f"refit: {refit_result.fits_written} symbol(s); "
              f"skipped (no data/theme): {refit_result.skipped}")
        return

    if attr_command == "score":
        from worker.attribution import score

        with engine.begin() as conn:
            score_result = score(conn, trade_date, now_utc=now,
                                 model_version=ATTRIBUTION_MODEL_VERSION, synthetic=pm)
        print(f"score: {score_result.rows_written} row(s) (synthetic={pm})")
        return

    if attr_command == "reconcile":
        from worker.reconcile import reconcile

        with engine.begin() as conn:
            recon_result = reconcile(conn, trade_date, now_utc=now,
                                     model_version=ATTRIBUTION_MODEL_VERSION)
        print(f"reconcile: revised {recon_result.revised}; "
              f"unchanged {len(recon_result.unchanged)}")
        return

    if attr_command == "signals":
        from worker.attribution_signals import compute_signals

        with engine.begin() as conn:
            sig = compute_signals(conn, trade_date, now_utc=now,
                                  model_version=ATTRIBUTION_MODEL_VERSION)
        print(f"signals: {sig.names_written} name(s), {sig.themes_written} theme(s)")
        return

    if attr_command == "maintenance":
        from worker.maintenance import surface_maintenance_flags

        with engine.begin() as conn:
            surfaced = surface_maintenance_flags(
                conn, DEV_USER_ID, trade_date, ATTRIBUTION_MODEL_VERSION
            )
        print(f"maintenance: {len(surfaced)} flag(s) surfaced")
        return

    raise SystemExit(
        "unknown attribution subcommand; try themes-seed|refit|score|reconcile|signals|"
        "maintenance|concordance"
    )


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


def _events_seed(date_arg: str | None, symbols_arg: str | None) -> None:
    from worker.events_seed import seed_events

    engine = get_engine()
    session_date = date.fromisoformat(date_arg) if date_arg else date.today()
    symbols = _resolve_symbols(symbols_arg, engine)
    if not symbols:
        raise SystemExit(
            "No symbols to seed. Pass --symbols AAPL,MSFT or add holdings to your book."
        )

    with engine.begin() as conn:
        written = seed_events(conn, session_date=session_date, symbols=symbols)

    print(f"events-seed: {written} event(s) around {session_date} for {len(symbols)} symbol(s)")


def _seed_premarket(date_arg: str | None) -> None:
    from worker.scheduler import ingest_premarket_for_session

    engine = get_engine()
    session_date = date.fromisoformat(date_arg) if date_arg else date.today()
    prior_session = calendar.previous_session(session_date)

    written = ingest_premarket_for_session(
        engine, session_date=session_date, prior_session=prior_session
    )

    print(f"seed-premarket: {written} quote(s) captured for {session_date}")


def _resolve_symbols(symbols_arg: str | None, engine: Engine) -> list[str]:
    """Held names *plus every sector benchmark*. The benchmarks are settable in
    the book UI but were never ingested, so §5's trailing-5d line had no bars to
    read (M14)."""
    if symbols_arg:
        return [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT symbol FROM holdings "
                "UNION "
                "SELECT DISTINCT benchmark_symbol FROM sectors "
                "WHERE benchmark_symbol IS NOT NULL "
                "ORDER BY symbol"
            )
        ).all()
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

    # Only the close path can return None (its quiet-session skip gate); the
    # open brief always sends, so the union comes from `assemble_and_store`.
    obj: BriefObject | None
    conn = engine.connect()
    trans = conn.begin()
    try:
        if kind == "open":
            # The open brief is *for* session_date but reads the prior close; it
            # has no skip gate and never returns None (docs/05).
            obj = assemble_open_and_store(
                conn,
                user_id,
                session_date,
                prior_session=calendar.previous_session(session_date),
                generated_at=datetime.now(UTC),
                narrator=narrator,
            )
        else:
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


def _schedule(once: bool, dry_run: bool, kind: str) -> None:
    from datetime import timedelta

    from worker import config
    from worker.scheduler import (
        next_kind_fire,
        run_open_session_job,
        run_scheduler,
        run_session_job,
    )

    if dry_run:
        delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
        now = datetime.now(UTC)
        print(
            f"schedule: now {now.isoformat()}  "
            f"(close = session close +{config.SEND_DELAY_MINUTES}min, "
            f"open = {config.OPEN_SEND_ET_HOUR:02d}:{config.OPEN_SEND_ET_MINUTE:02d} ET fixed)"
        )
        for _ in range(8):
            when, kind = next_kind_fire(now, delay)
            et = when.astimezone(calendar.ET)
            session = "session" if calendar.is_session(et.date()) else "non-session"
            print(
                f"  {kind:5}  {when.isoformat()}   "
                f"{et:%a %Y-%m-%d %H:%M} ET   ({session})"
            )
            now = when + timedelta(seconds=1)
        return

    engine = get_engine()
    if once:
        now = datetime.now(UTC)
        if kind == "open":
            outcome = run_open_session_job(engine, now_utc=now)
        else:
            outcome = run_session_job(engine, now_utc=now)
        print(f"schedule --once ({kind}): {outcome}")
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
