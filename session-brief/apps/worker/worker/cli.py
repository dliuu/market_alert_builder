"""Worker CLI entrypoint."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from contracts.brief import BriefObject
from worker import calendar
from worker.assemble import _read_closes, assemble_and_store
from worker.assemble_open import assemble_open_and_store
from worker.compute import compute_and_store
from worker.constants import (
    BENCHMARK_SYMBOL,
    DEV_USER_ID,
    FDN_TAPE_IDENTIFIERS,
    OPTIONS_INDICES,
)
from worker.db import get_engine
from worker.ingest import ingest_daily_bars
from worker.normalize import normalize_bars
from worker.options_ingest import pull_options, rebuild_options
from worker.providers.cboe import (
    FEED_ERRORS as CBOE_FEED_ERRORS,
)
from worker.providers.cboe import (
    STORED_MAX_DTE,
    CboeClient,
    filter_payload_for_storage,
    parse_chain,
    parse_index_csv,
)
from worker.providers.fdn import FEED_ERRORS, FdnClient, _safe_error
from worker.providers.synthetic import SyntheticOptionsProvider
from worker.providers.tiingo import TiingoProvider
from worker.volatility import ExpiryIv, iv30_from_chain, select_strip


def main() -> None:
    parser = argparse.ArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("hello", help="smoke check that the worker boots")

    backfill = sub.add_parser("backfill", help="ingest + normalize daily bars")
    backfill.add_argument(
        "--symbols", help="comma-separated tickers; defaults to the symbols in your book"
    )
    backfill.add_argument("--days", type=int, default=90, help="look-back window (default 90)")
    backfill.add_argument(
        "--market", default="us", choices=("us", "cn"), help="which side to backfill (default us)"
    )

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

    fdn_probe = sub.add_parser(
        "fdn-probe",
        help="verify FDN_API_KEY, identifier mapping, and feed assumptions (read-only)",
    )
    fdn_probe.add_argument("--symbols", help="held symbols to probe, comma-separated")

    tiingo_cn_probe = sub.add_parser(
        "tiingo-cn-probe",
        help="verify TIINGO_API_KEY and CN ticker format assumptions (read-only)",
    )
    tiingo_cn_probe.add_argument(
        "--symbols", help="CN symbols to probe, comma-separated (defaults to a fixed sample)"
    )

    brief = sub.add_parser("brief", help="assemble a BriefObject for a session")
    brief.add_argument(
        "--kind",
        default="close",
        choices=("open", "close", "open_cn", "close_cn"),
        help="brief kind",
    )
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
        choices=("open", "close", "open_cn", "close_cn"),
        help="which job --once runs (default close); ignored by the blocking loop",
    )

    send = sub.add_parser("send", help="render a stored brief and email it via Resend")
    send.add_argument(
        "--kind",
        default="close",
        choices=("open", "close", "open_cn", "close_cn"),
        help="brief kind",
    )
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

    options = sub.add_parser("options", help="Cboe options chains and IV (M21)")
    opt_sub = options.add_subparsers(dest="opt_command")
    o_probe = opt_sub.add_parser(
        "probe",
        help="fetch Cboe's delayed chain and index history; print the strip and IV30 (read-only)",
    )
    o_probe.add_argument(
        "--symbols", help="symbols to probe, comma-separated; defaults to the book's held names"
    )
    o_probe.add_argument("--file", help="parse a saved chain JSON instead of fetching")
    o_probe.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest session")
    o_probe.add_argument(
        "--indices",
        default=",".join(OPTIONS_INDICES),
        help="Cboe index histories to fetch, comma-separated; '' to skip",
    )
    o_probe.add_argument("--save", help="directory to write each verbatim chain JSON into")
    o_pull = opt_sub.add_parser("pull", help="pull, band and store chains and the VIX family")
    o_pull.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest session")
    o_pull.add_argument("--symbols", help="comma-separated; defaults to the book")
    o_rebuild = opt_sub.add_parser("rebuild", help="replay stored chains (no network)")
    o_rebuild.add_argument("--date", help="session date YYYY-MM-DD; defaults to the latest session")

    args = parser.parse_args()

    if args.command == "catalysts":
        _catalysts(args.cat_command, date_arg=getattr(args, "date", None),
                   symbols_arg=getattr(args, "symbols", None))
        return

    if args.command == "options":
        _options(
            args.opt_command,
            date_arg=getattr(args, "date", None),
            symbols_arg=getattr(args, "symbols", None),
            file_arg=getattr(args, "file", None),
            indices_arg=getattr(args, "indices", ",".join(OPTIONS_INDICES)),
            save_dir=getattr(args, "save", None),
        )
        return

    if args.command == "backfill":
        _backfill(symbols_arg=args.symbols, days=args.days, market=args.market)
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

    if args.command == "fdn-probe":
        _fdn_probe_cmd(symbols_arg=args.symbols)
        return

    if args.command == "tiingo-cn-probe":
        _tiingo_cn_probe_cmd(symbols_arg=args.symbols)
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
    from worker.catalysts import book_floats, held_symbols, next_earnings, rebuild_signals
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
                if symbols_arg else held_symbols(conn, DEV_USER_ID)
            )
            # Live when the key is set (M16's switch), synthetic otherwise —
            # the same rule the open job applies to the pre-market feed.
            from worker import config
            if config.FDN_API_KEY:
                from worker.providers.fdn import FdnCatalystProvider, FdnClient

                client = FdnClient()
                try:
                    counts = ingest_catalysts(
                        conn, FdnCatalystProvider(client), symbols, as_of=session_date
                    )
                finally:
                    client.close()
            else:
                counts = ingest_catalysts(
                    conn, SyntheticCatalystProvider(session_date), symbols, as_of=session_date
                )
            print(f"catalysts ingest {session_date}: {counts}")
            return

        if cat_command in ("detect", "rebuild"):
            floats = book_floats(conn, session_date)
            stored = rebuild_signals(
                conn, model_version=CATALYST_MODEL_VERSION,
                as_of=session_date, earnings=next_earnings(conn), floats=floats,
            )
            print(f"catalysts {cat_command} {session_date}: {stored} signals")
            return

    raise SystemExit("usage: catalysts {ingest|detect|rebuild} --date YYYY-MM-DD")


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


def _backfill(symbols_arg: str | None, days: int, market: str) -> None:
    engine = get_engine()
    symbols = _resolve_symbols(symbols_arg, engine, market=market.upper())
    if not symbols:
        raise SystemExit(
            "No symbols to backfill. Pass --symbols AAPL,MSFT or add holdings to your book."
        )

    end = date.today()
    start = end - timedelta(days=days)

    if market == "cn":
        # CN logic stays in worker_cn (separation rule); cli.py only routes.
        from worker_cn.backfill import backfill_cn

        try:
            result = backfill_cn(engine, symbols, start, end)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None
        print(
            f"backfill (cn): {len(symbols)} symbol(s) {start}..{end} — "
            f"{result.written} new payload(s), {result.bars} bar(s) normalized"
        )
        return

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


def _fdn_probe_cmd(symbols_arg: str | None) -> None:
    engine = get_engine()
    symbols = _resolve_symbols(symbols_arg, engine)
    # fdn-probe is the day-one diagnostic — often the first thing run, quite
    # possibly before the key arrives — so a missing FDN_API_KEY is guidance,
    # not a crash; the raw RuntimeError from FdnClient() would print a
    # traceback for what is really a usage error.
    try:
        client = FdnClient()
    except RuntimeError:
        raise SystemExit(
            "FDN_API_KEY is not set. For local runs, add it to the repo-root "
            ".env; for the deployed worker, `fly secrets set FDN_API_KEY=...`. "
            "Without it, the open brief runs on the synthetic feed — a valid "
            "state, not an error."
        ) from None
    _fdn_probe(client, symbols=symbols)


# --- options (M21) ---


def _options(
    opt_command: str | None,
    *,
    date_arg: str | None,
    symbols_arg: str | None,
    file_arg: str | None = None,
    indices_arg: str = ",".join(OPTIONS_INDICES),
    save_dir: str | None = None,
) -> None:
    if opt_command == "probe":
        _options_probe_cmd(
            symbols_arg=symbols_arg,
            file_arg=file_arg,
            date_arg=date_arg,
            indices_arg=indices_arg,
            save_dir=save_dir,
        )
    elif opt_command == "pull":
        _options_pull(date_arg=date_arg, symbols_arg=symbols_arg)
    elif opt_command == "rebuild":
        _options_rebuild(date_arg=date_arg)
    else:
        raise SystemExit("usage: options {probe|pull|rebuild}")


def _options_pull(*, date_arg: str | None, symbols_arg: str | None) -> None:
    """Live Cboe when ``OPTIONS_LIVE`` is set, else the synthetic chain priced
    off the session's ``bars_daily`` closes. Feed failures are reported on the
    summary and do not fail the run; a database error raises."""
    from worker import config

    session_date = _options_session_date(date_arg)
    engine = get_engine()
    symbols = _resolve_symbols(symbols_arg, engine)
    with engine.begin() as conn:
        if config.OPTIONS_LIVE:
            client = CboeClient()
            try:
                result = pull_options(conn, client, symbols, session_date)
            finally:
                client.close()
        else:
            closes = _read_closes(conn, symbols, session_date)
            result = pull_options(
                conn, SyntheticOptionsProvider(closes, session_date), symbols, session_date
            )
    print(
        f"options pull {session_date}: {len(result.symbols_ok)} symbols ok "
        f"{list(result.symbols_ok)}, indices ok {list(result.indices_ok)}, "
        f"{result.raw_rows} raw rows"
    )
    for name, reason in {**result.symbols_failed, **result.indices_failed}.items():
        print(f"✗ {name}: {reason}")


def _options_rebuild(*, date_arg: str | None) -> None:
    session_date = _options_session_date(date_arg)
    with get_engine().begin() as conn:
        written = rebuild_options(conn, session_date)
    print(f"options rebuild {session_date}: {written} snapshot rows")


def _options_probe_cmd(
    symbols_arg: str | None,
    file_arg: str | None,
    date_arg: str | None,
    indices_arg: str,
    save_dir: str | None,
) -> None:
    session_date = _options_session_date(date_arg)
    if file_arg:
        text_ = Path(file_arg).read_text()
        payload = json.loads(text_, parse_float=Decimal)
        print(f"options probe: file={file_arg} session={session_date}")
        _options_report_chain(payload, session_date, raw_bytes=len(text_.encode()))
        return
    if symbols_arg:
        symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    else:
        symbols = _resolve_symbols(None, get_engine())
    indices = [s.strip() for s in indices_arg.split(",") if s.strip()]
    client = CboeClient()
    try:
        _options_probe(
            client,
            symbols=symbols,
            indices=indices,
            session_date=session_date,
            save_dir=Path(save_dir) if save_dir else None,
        )
    finally:
        client.close()


def _options_session_date(date_arg: str | None) -> date:
    """The session the chain describes: the ET date if it is a session, else
    the previous one — a Saturday run reads Friday's closing book."""
    if date_arg:
        return date.fromisoformat(date_arg)
    today = calendar.today_et(datetime.now(UTC))
    return today if calendar.is_session(today) else calendar.previous_session(today)


def _options_probe(
    client: CboeClient,
    *,
    symbols: list[str],
    indices: list[str],
    session_date: date,
    save_dir: Path | None = None,
) -> None:
    """Day-one verification of the Cboe feed (M21 spike): fetch each held
    name's chain, print what the strip and IV30 would be, and fetch the index
    histories. Read-only — no database writes — and every symbol is
    independently fault-tolerant so one bad chain never stops the rest.
    Always exits 0; a human reads the ✓/✗ lines."""
    print(f"options probe: now={datetime.now(UTC).isoformat()} session={session_date}")
    for symbol in symbols:
        try:
            payload = client.chain(symbol)
        except CBOE_FEED_ERRORS as exc:
            print(f"✗ {symbol}: {_safe_error(exc)}")
            continue
        _endpoint, _symbol, text_ = client.captured[-1]
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{symbol}.json").write_text(text_)
        try:
            _options_report_chain(payload, session_date, raw_bytes=len(text_.encode()))
        except CBOE_FEED_ERRORS as exc:
            print(f"✗ {symbol}: parse failed: {_safe_error(exc)}")
    for index in indices:
        try:
            bars = parse_index_csv(client.index_history(index))
        except CBOE_FEED_ERRORS as exc:
            print(f"✗ {index}: {_safe_error(exc)}")
            continue
        if not bars:
            print(f"✗ {index}: empty history")
            continue
        print(
            f"✓ {index}: {len(bars)} rows, {bars[0].session_date} → {bars[-1].session_date}, "
            f"last close {bars[-1].c}"
        )
        # The history file lags the session by a day on the evening of the
        # session itself (live probe, 2026-10-20: VIX_History ended 10-19 at
        # 23:15 UTC while VIX9D and VIX3M carried 10-20). The delayed index
        # quote carries today's level, so the two together are complete.
        try:
            quote = client.index_quote(index)
            level = quote["data"]
            print(
                f"  {index} delayed quote: close={level.get('close')} "
                f"current={level.get('current_price')} snapshot {quote.get('timestamp')} UTC"
            )
        except CBOE_FEED_ERRORS as exc:
            print(f"✗ {index} delayed quote: {_safe_error(exc)}")


def _options_report_chain(payload: dict[str, Any], session_date: date, *, raw_bytes: int) -> None:
    underlying, contracts = parse_chain(payload)
    expiries = sorted({c.expiry for c in contracts})
    spot = underlying.close
    stamp = underlying.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"✓ {underlying.symbol}: {len(contracts)} contracts, {len(expiries)} expiries "
        f"({expiries[0] if expiries else 'none'} → {expiries[-1] if expiries else 'none'}), "
        f"snapshot {stamp}"
    )
    print(
        f"  spot close={spot} current={underlying.current_price} "
        f"prev_close={underlying.prev_day_close}   cboe iv30={_vol(underlying.iv30)}"
    )
    iv30 = iv30_from_chain(contracts, spot, session_date)
    drift = (
        f"{iv30.iv30 - underlying.iv30:+.2f}"
        if iv30.iv30 is not None and underlying.iv30 is not None
        else "n/a"
    )
    print(
        f"  IV30 ours={_vol(iv30.iv30)} n={iv30.n} drift vs cboe={drift}  "
        f"near={_leg(iv30.near)} far={_leg(iv30.far)} dropped={list(iv30.dropped) or '[]'}"
    )
    stored = filter_payload_for_storage(payload, session_date)
    stored_bytes = len(json.dumps(stored, default=str).encode())
    print(
        f"  stored payload: {len(stored['data']['options'])} contracts ≤{STORED_MAX_DTE} dte, "
        f"{stored_bytes / 1024:,.0f} KB of {raw_bytes / 1024:,.0f} KB"
    )
    header = (
        f"  {'tenor':>5} {'dte':>3} {'expiry':<10} {'rule':<5} {'strike':>8} {'money':>7} "
        f"{'bid':>7} {'ask':>7} {'mid':>7} {'prem%':>6} {'tv%':>6} {'iv':>6} {'delta':>5} "
        f"{'OI':>6} {'vol':>6}  flags"
    )
    print(header)
    for cell in select_strip(contracts, spot, session_date):
        c = cell.contract
        if c is None:
            print(f"  {cell.tenor:>5} {'—':>3} {'—':<10} {cell.rule:<5}  no two-sided call")
            continue
        flags = " ".join(
            f for f, on in (("MISS", cell.target_miss), ("COLLAPSED", cell.tenor_collapsed)) if on
        )
        print(
            f"  {cell.tenor:>5} {cell.dte:>3} {c.expiry.isoformat():<10} {cell.rule:<5} "
            f"{c.strike:>8.2f} {_pct_d(cell.moneyness):>7} {c.bid:>7.2f} {c.ask:>7.2f} "
            f"{c.mid:>7.2f} {_pct_d(cell.premium_pct):>6} {_pct_d(cell.time_value_pct):>6} "
            f"{_vol(c.iv):>6} {_d(c.delta, 2):>5} {c.open_interest:>6} {c.volume:>6}  {flags}"
        )


def _leg(leg: ExpiryIv | None) -> str:
    if leg is None:
        return "none"
    return f"{leg.expiry}({leg.dte}d iv {leg.iv:.2f} n{leg.n})"


def _vol(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _d(value: Decimal | None, places: int) -> str:
    return f"{value:.{places}f}" if value is not None else "n/a"


def _pct_d(value: Decimal | None) -> str:
    return f"{value * 100:+.1f}%" if value is not None else "n/a"


def _tiingo_cn_probe_cmd(symbols_arg: str | None) -> None:
    # CN business logic (candidate formats, calendar comparisons) lives in
    # worker_cn.probe per cn/README.md's separation rule; this function only
    # routes and handles the keyless-usage-error case, mirroring _fdn_probe_cmd.
    from worker_cn.probe import DEFAULT_SYMBOLS, tiingo_cn_probe

    symbols = (
        [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        if symbols_arg
        else DEFAULT_SYMBOLS
    )
    try:
        provider = TiingoProvider()
    except RuntimeError:
        raise SystemExit(
            "TIINGO_API_KEY is not set. For local runs, add it to the repo-root "
            ".env; for the deployed worker, `fly secrets set TIINGO_API_KEY=...`. "
            "Without it, CN bars stay on the synthetic feed — a valid state, "
            "not an error."
        ) from None
    tiingo_cn_probe(provider, symbols=symbols)


def _fdn_probe(client: FdnClient, *, symbols: list[str]) -> None:
    """Day-one verification (M16 Task 8): the handful of vendor assumptions
    that could not be checked offline. Read-only — no database writes, no
    raw_payloads capture — and every check is independently fault-tolerant so
    one bad endpoint never stops the rest from reporting. Always exits 0;
    a human reads the ✓/✗ lines and decides whether FDN_TAPE_IDENTIFIERS or
    the parsing code needs an edit before the first live send."""
    print(f"fdn-probe: now={datetime.now(UTC).isoformat()}")

    # 1. Every FDN_TAPE_IDENTIFIERS entry: fetch, record count, first
    # trading_symbol, raw keys — verifies each futures/index/forex identifier
    # guess. The keys matter as much as the count: `_quote_rows` assumes
    # `price` and `change` on *both* index-quotes and stock-quotes, and only
    # the latter is dumped by check 4 below.
    for symbol, (endpoint, identifier) in FDN_TAPE_IDENTIFIERS.items():
        label = f"tape {symbol} -> {endpoint} identifier={identifier}"
        try:
            param = "identifier" if endpoint == "futures-prices" else "identifiers"
            records = client.fetch(endpoint, **{param: identifier})
            first_symbol = records[0].get("trading_symbol") if records else "n/a"
            keys = sorted(records[0].keys()) if records else []
            print(
                f"✓ {label}: {len(records)} record(s), "
                f"first trading_symbol={first_symbol!r}, keys={keys}"
            )
        except FEED_ERRORS as exc:
            print(f"✗ {label}: {_safe_error(exc)}")

    # 2. latest-prices for one held symbol: the record count and the two most
    # recent `time` values next to datetime.now(UTC). Two open questions in
    # one line — the UTC assumption in `_parse_fdn_time` by eye, and the
    # pagination question (I3): a week of minute bars is ~1,950 records
    # against a documented 300-record cap, so a count at or near 300 whose
    # latest `time` is days old means the response is oldest-first and
    # truncated, `FdnPremarketProvider.get_latest_prices` finds an empty
    # window for *every* held name, and §3 disappears from the brief.
    if symbols:
        held = symbols[0]
        label = f"latest-prices {held} (UTC assumption + 300-record cap)"
        try:
            records = client.fetch("latest-prices", identifier=held)
            times = sorted(str(r.get("time")) for r in records)[-2:]
            print(
                f"✓ {label}: {len(records)} record(s), latest times={times}  "
                f"now(UTC)={datetime.now(UTC).isoformat()}"
            )
        except FEED_ERRORS as exc:
            print(f"✗ {label}: {_safe_error(exc)}")
    else:
        print("✗ latest-prices: no held symbols to probe")

    # 3. futures-prices for ES: does a bar dated *today* exist at pre-open?
    # `_futures_rows` requires one and drops the symbol otherwise, so a "no"
    # costs §2 three of its six fixed rows (ES=F/NQ=F/CL=F). The spec's own
    # verified facts say these are daily bars with "no live overnight print",
    # which makes "no" the likely answer — hence the explicit verdict rather
    # than two dates for the reader to compare.
    label = "futures-prices ES (session-dated bar)"
    try:
        bars = client.fetch("futures-prices", identifier="ES")
        bars.sort(key=lambda r: str(r.get("date", "")), reverse=True)
        latest_date = str(bars[0].get("date")) if bars else "n/a"
        today_iso = date.today().isoformat()
        verdict = (
            "session-dated bar EXISTS → §2 futures rows render"
            if latest_date == today_iso
            else "NO session-dated bar → ES=F/NQ=F/CL=F are DROPPED from §2"
        )
        print(
            f"✓ {label}: {len(bars)} bar(s), latest bar date={latest_date}  "
            f"today={today_iso}  → {verdict}"
        )
    except FEED_ERRORS as exc:
        print(f"✗ {label}: {_safe_error(exc)}")

    # 4. stock-quotes for one proxy ETF: raw record keys — verifies the
    # price/change field assumption.
    label = "stock-quotes EWT (raw record keys)"
    try:
        records = client.fetch("stock-quotes", identifiers="EWT")
        keys = sorted(records[0].keys()) if records else []
        print(f"✓ {label}: keys={keys}")
    except FEED_ERRORS as exc:
        print(f"✗ {label}: {_safe_error(exc)}")

    # 5. Each calendar + latest-news for today: record counts (also proves
    # the key's tier covers Premium).
    today = date.today().isoformat()
    for endpoint in ("earnings-calendar", "dividends-calendar", "economic-calendar"):
        label = f"{endpoint} {today}"
        try:
            records = client.fetch(endpoint, date=today)
            print(f"✓ {label}: {len(records)} record(s)")
        except FEED_ERRORS as exc:
            print(f"✗ {label}: {_safe_error(exc)}")

    label = f"latest-news {today}"
    try:
        records = client.fetch("latest-news", date=today)
        print(f"✓ {label}: {len(records)} record(s)")
    except FEED_ERRORS as exc:
        print(f"✗ {label}: {_safe_error(exc)}")

    # 6. latest-news page size (Task 7 review finding): news_fdn.py hardcodes
    # offset=page*10, assuming 10 records per page. Confirm live, and confirm
    # the `date` param is accepted (not silently ignored).
    label = "latest-news page size (news_fdn.py assumes 10/page)"
    try:
        records = client.fetch("latest-news", date=today, offset="0")
        print(
            f"✓ {label}: expected=10  got={len(records)}  "
            f"date param accepted (200 for date={today})"
        )
    except FEED_ERRORS as exc:
        print(f"✗ {label}: {_safe_error(exc)}")

    # 7. Does latest-news accept a per-symbol filter? (I6.) `fetch_held_news`
    # pulls 3 pages × 10 *market-wide* articles and keeps whatever mentions a
    # held name — but 30 market-wide headlines will rarely touch any of ~10
    # holdings, so the §3 has_news gate and the narration headlines are
    # almost always empty. If `identifier` is honoured, news should be fetched
    # per symbol instead. The vendor's other endpoints use `identifier`, so
    # that is the guess; an unknown param is typically ignored rather than
    # rejected, which is exactly why this checks the *records*, not the status
    # code — an all-mentions response means honoured, anything else means
    # silently dropped.
    if symbols:
        held = symbols[0]
        label = f"latest-news identifier={held} (per-symbol filter honoured?)"
        try:
            records = client.fetch("latest-news", date=today, identifier=held)
            mentions = sum(
                1 for r in records
                if held in {str(s) for s in (r.get("trading_symbols") or [])}
            )
            if records and mentions == len(records):
                verdict = "HONOURED → switch news_fdn.py to per-symbol fetching"
            else:
                verdict = (
                    "IGNORED (param silently dropped) → keep market-wide paging "
                    "and expect an empty news gate, or find the real filter param"
                )
            print(f"✓ {label}: {len(records)} record(s), {mentions} mention {held} → {verdict}")
        except FEED_ERRORS as exc:
            print(f"✗ {label}: {_safe_error(exc)}")
    else:
        print("✗ latest-news per-symbol filter: no held symbols to probe")


def _resolve_symbols(symbols_arg: str | None, engine: Engine, *, market: str = "US") -> list[str]:
    """Held names *plus every sector benchmark*, scoped to one market — a CN
    backfill must never pull in the US book's symbols (and vice versa on a
    mixed book). Mirrors ``worker.scheduler.book_symbols``'s market filter and
    benchmark union. The benchmarks are settable in the book UI but were never
    ingested, so §5's trailing-5d line had no bars to read (M14)."""
    if symbols_arg:
        return [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    if market == "CN":
        from worker_cn.constants import CN_BENCHMARK

        benchmark_symbol = CN_BENCHMARK
    else:
        benchmark_symbol = BENCHMARK_SYMBOL
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT h.symbol FROM holdings h "
                "JOIN sectors s ON s.id = h.sector_id AND s.user_id = h.user_id "
                "WHERE s.market = :market "
                "UNION "
                "SELECT DISTINCT benchmark_symbol FROM sectors "
                "WHERE benchmark_symbol IS NOT NULL AND market = :market "
                "UNION "
                "SELECT :benchmark "
                "ORDER BY symbol"
            ),
            {"market": market, "benchmark": benchmark_symbol},
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
        elif kind == "close_cn":
            # CN logic stays in worker_cn (separation rule); cli.py only routes.
            from worker_cn.assemble import assemble_cn_close_and_store

            obj = assemble_cn_close_and_store(conn, user_id, session_date)
        elif kind == "open_cn":
            # CN logic stays in worker_cn (separation rule); cli.py only routes.
            from worker_cn.assemble import assemble_cn_open_and_store
            from worker_cn.calendar import CN

            obj = assemble_cn_open_and_store(
                conn,
                user_id,
                session_date,
                prior_session=CN.previous_session(session_date),
                generated_at=datetime.now(UTC),
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
        from worker_cn import config as cn_config
        from worker_cn.calendar import CN

        delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
        now = datetime.now(UTC)
        print(
            f"schedule: now {now.isoformat()}  "
            f"(close = session close +{config.SEND_DELAY_MINUTES}min, "
            f"open = {config.OPEN_SEND_ET_HOUR:02d}:{config.OPEN_SEND_ET_MINUTE:02d} ET fixed, "
            f"close_cn = session close +{cn_config.CN_SEND_DELAY_MINUTES}min, "
            f"open_cn = {cn_config.CN_OPEN_SEND_HOUR:02d}:{cn_config.CN_OPEN_SEND_MINUTE:02d} "
            f"CST fixed)"
        )
        for _ in range(8):
            when, kind = next_kind_fire(now, delay)
            # Each kind reads in its own market's wall clock — an ET-only
            # display would mislabel a 15:20 CST CN close as "03:20 ET".
            if kind in ("open_cn", "close_cn"):
                local, tz_label = when.astimezone(CN.tz), "CST"
                is_session = CN.is_session
            else:
                local, tz_label = when.astimezone(calendar.ET), "ET"
                is_session = calendar.is_session
            session = "session" if is_session(local.date()) else "non-session"
            print(
                f"  {kind:8}  {when.isoformat()}   "
                f"{local:%a %Y-%m-%d %H:%M} {tz_label}   ({session})"
            )
            now = when + timedelta(seconds=1)
        return

    engine = get_engine()
    if once:
        now = datetime.now(UTC)
        if kind == "open":
            outcome = run_open_session_job(engine, now_utc=now)
        elif kind == "close":
            outcome = run_session_job(engine, now_utc=now)
        elif kind == "open_cn":
            from worker_cn.scheduler import run_cn_open_session_job

            outcome = run_cn_open_session_job(engine, now_utc=now)
        else:  # close_cn
            from worker_cn.scheduler import run_cn_close_session_job

            outcome = run_cn_close_session_job(engine, now_utc=now)
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
