"""M10 — the scheduler and dead-man's switch.

A long-running process (Fly.io, no cold starts — docs/02) that fires once per
calendar day and drives the close pipeline end to end: poll for today's EOD bar
→ compute → assemble → narrate → send. It is the only unattended entrypoint;
everything it calls already exists and is tested in isolation.

Two properties make it trustworthy for "five sessions untouched, one holiday
skipped" (docs/08 M10):

- **The calendar decides, not a cron.** Each run asks ``calendar.is_session``;
  a holiday is skipped by the pipeline never running, and the next fire is placed
  at the *next* day's real close, so a half-day moves the send with it.
- **Every run pings the dead-man's switch.** Success (including a correct holiday
  skip) pings Healthchecks; an uncaught failure pings ``/fail`` and re-raises. A
  worker that dies simply stops pinging, and the check goes red — which is how
  you learn about Thursday's failure on Thursday, not Monday (docs/02).

The pure parts (``fire_time``/``next_fire``) and the job body (``run_session_job``,
which takes an injected ``now``) are unit-tested without a clock or a network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time  # `time` is already the stdlib module here
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from worker import calendar, config
from worker.constants import ATTRIBUTION_MODEL_VERSION, BENCHMARK_SYMBOL, DEV_USER_ID
from worker.fundamentals import EdgarProvider
from worker.providers.base import MarketDataProvider, PremarketProvider

UTC = ZoneInfo("UTC")
ET = calendar.ET  # America/New_York — the tz all wall-clock scheduling uses


# --- Dead-man's switch (Healthchecks.io) ------------------------------------
#
# Pings are best-effort: a monitoring outage must never take down the send. Both
# swallow network errors by design — the exception here is the feature, like the
# narration guard (D19), not a hedge.


def ping_success(url: str) -> None:
    if not url:
        return
    try:
        httpx.get(url, timeout=10.0)
    except httpx.HTTPError:
        pass


def ping_fail(url: str, detail: str) -> None:
    if not url:
        return
    try:
        httpx.post(f"{url}/fail", content=detail.encode("utf-8"), timeout=10.0)
    except httpx.HTTPError:
        pass


# --- Fire-time scheduling (pure) --------------------------------------------


def fire_time(d: date, delay: timedelta) -> datetime:
    """When the run for calendar date ``d`` fires: its close (real session close,
    or the nominal bell on a non-session day) plus ``delay``."""
    return calendar.close_or_standard(d) + delay


def next_fire(now_utc: datetime, delay: timedelta) -> datetime:
    """The next close fire instant strictly after ``now_utc``. Walks forward from
    today's ET date until a day's fire time is in the future — so a run that
    finishes at/after its own fire time rolls cleanly to the next day."""
    d = calendar.today_et(now_utc)
    for _ in range(8):  # today + a long weekend/holiday stretch is well under 8
        ft = fire_time(d, delay)
        if ft > now_utc:
            return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no fire time within 8 days of {now_utc}")  # pragma: no cover


def _et_fire(d: date, at_et: clock_time) -> datetime:
    return datetime.combine(d, at_et, tzinfo=ET).astimezone(UTC)


def next_session_fire(now_utc: datetime, at_et: clock_time) -> datetime:
    """Next trading-session day at ``at_et`` (ET wall-clock), strictly after now."""
    d = calendar.today_et(now_utc)
    for _ in range(8):
        if calendar.is_session(d):
            ft = _et_fire(d, at_et)
            if ft > now_utc:
                return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no session fire within 8 days of {now_utc}")  # pragma: no cover


def next_weekly_fire(now_utc: datetime, weekday: int, at_et: clock_time) -> datetime:
    """Next ``weekday`` (Mon=0..Sun=6) at ``at_et`` (ET), strictly after now."""
    d = calendar.today_et(now_utc)
    for _ in range(8):
        if d.weekday() == weekday:
            ft = _et_fire(d, at_et)
            if ft > now_utc:
                return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no weekly fire within 8 days of {now_utc}")  # pragma: no cover


def open_fire_time(d: date) -> datetime | None:
    """When the open brief for session ``d`` fires: a **fixed 08:15 ET**, or
    ``None`` if ``d`` isn't a trading session.

    This is deliberately *not* the close fire's construction. The close is
    anchored on the real session close plus a delay, which is what makes a
    half-day move the send automatically (D20). The open brief has no such
    anchor — it is read before the bell, and a half-day is still a normal
    morning — so it is a wall-clock time converted through ``America/New_York``
    (invariant 8, so DST doesn't shift it twice a year).

    Unlike the close fire it also does not run on non-sessions. The close fire
    keeps firing daily because it carries the dead-man's-switch heartbeat; the
    open brief has its own check and nothing to say on a holiday.
    """
    if not calendar.is_session(d):
        return None
    et_time = clock_time(config.OPEN_SEND_ET_HOUR, config.OPEN_SEND_ET_MINUTE)
    return datetime.combine(d, et_time, tzinfo=ET).astimezone(UTC)


def next_kind_fire(now_utc: datetime, delay: timedelta) -> tuple[datetime, str]:
    """The next fire of *either* kind, as ``(instant, kind)``.

    One loop over both schedules rather than two independent triggers: the
    scheduler stays a single self-rescheduling one-shot (D20's shape), and the
    ordering between the morning and evening runs is explicit rather than
    emergent from two timers racing.
    """
    d = calendar.today_et(now_utc)
    for _ in range(8):
        candidates: list[tuple[datetime, str]] = [(fire_time(d, delay), "close")]
        open_at = open_fire_time(d)
        if open_at is not None:
            candidates.append((open_at, "open"))

        future = sorted(c for c in candidates if c[0] > now_utc)
        if future:
            return future[0]
        d = d + timedelta(days=1)
    raise RuntimeError(f"no fire time within 8 days of {now_utc}")  # pragma: no cover


# --- The daily job ----------------------------------------------------------


def sector_benchmarks(conn: Connection, user_id: str) -> list[str]:
    """Every benchmark set in the book. Settable in the UI since M1, and read by
    two callers: the ingest universe and §2's foreign proxies."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT benchmark_symbol FROM sectors "
            "WHERE user_id = :u AND benchmark_symbol IS NOT NULL"
        ),
        {"u": user_id},
    ).all()
    return sorted(str(r[0]) for r in rows)


def book_symbols(conn: Connection, user_id: str = DEV_USER_ID) -> list[str]:
    """The symbols to refresh: every held name, every sector benchmark, plus the
    market benchmark. SPY is *always* needed for the vs-SPY line even when it
    isn't in the book — the manual ``backfill`` omits it, which is a latent gap
    the unattended run can't afford. The sector benchmarks are the same trap one
    level down: settable in the book UI since M1, never ingested, so the open
    brief's §5 trailing-5d line had nothing to read (M14)."""
    rows = conn.execute(
        text("SELECT DISTINCT symbol FROM holdings WHERE user_id = :u"),
        {"u": user_id},
    ).all()
    held = {str(r[0]) for r in rows}
    return sorted(held | set(sector_benchmarks(conn, user_id)) | {BENCHMARK_SYMBOL})


def _bars_present(engine: Engine, symbols: list[str], session_date: date) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT symbol FROM bars_daily "
                "WHERE session_date = :d AND symbol = ANY(:syms)"
            ),
            {"d": session_date, "syms": symbols},
        ).all()
    return {str(r[0]) for r in rows}


def ensure_todays_bars(
    engine: Engine,
    provider: MarketDataProvider,
    symbols: list[str],
    session_date: date,
    *,
    timeout_s: int,
    interval_s: int,
    now_monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """Poll Tiingo until every symbol has a bar for ``session_date`` or the
    timeout elapses. Ingest is idempotent (D13), so re-fetching a short window is
    cheap. Returns the set still missing at the end — empty means all present."""
    from worker.ingest import ingest_daily_bars
    from worker.normalize import normalize_bars

    start = session_date - timedelta(days=7)  # small top-up window; history already stored
    deadline = now_monotonic() + timeout_s
    while True:
        with engine.begin() as conn:
            ingest_daily_bars(conn, provider, symbols, start, session_date)
            normalize_bars(conn, symbols)
        missing = set(symbols) - _bars_present(engine, symbols, session_date)
        if not missing or now_monotonic() >= deadline:
            return missing
        sleep(interval_s)


def run_session_job(
    engine: Engine,
    *,
    now_utc: datetime,
    user_id: str = DEV_USER_ID,
    recipient: str | None = None,
    sender: str | None = None,
    provider: MarketDataProvider | None = None,
    healthcheck_url: str | None = None,
    poll: bool = True,
) -> str:
    """One day's go/no-go run. Returns an outcome tag: ``skipped-holiday``,
    ``skipped-quiet`` (nothing moved >1%), or ``sent``. Pings the dead-man's
    switch on the way out; a crash pings ``/fail`` and re-raises."""
    from worker.assemble import assemble_and_store
    from worker.deliver import deliver_brief
    from worker.narrate import default_narrator

    hc = config.HEALTHCHECKS_URL if healthcheck_url is None else healthcheck_url
    session_date = calendar.today_et(now_utc)
    try:
        if not calendar.is_session(session_date):
            print(f"scheduler {session_date}: not a trading session — skipping.")
            ping_success(hc)
            return "skipped-holiday"

        with engine.connect() as conn:
            symbols = book_symbols(conn, user_id)
        if poll:
            prov = provider or _default_provider()
            missing = ensure_todays_bars(
                engine,
                prov,
                symbols,
                session_date,
                timeout_s=config.BAR_POLL_TIMEOUT_S,
                interval_s=config.BAR_POLL_INTERVAL_S,
            )
            if missing:
                print(f"scheduler {session_date}: bars still missing after poll: {sorted(missing)}")

        # Assemble in its own transaction so the briefs row is committed before
        # delivery reads it back (deliver renders from the persisted body).
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                obj = assemble_and_store(
                    conn, user_id, session_date, "close", narrator=default_narrator()
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise

        if obj is None:
            print(f"scheduler {session_date}: quiet session — nothing moved >1%, no send.")
            ping_success(hc)
            return "skipped-quiet"

        to = recipient or config.BRIEF_RECIPIENT
        frm = sender or config.BRIEF_FROM
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                result = deliver_brief(
                    conn,
                    user_id=user_id,
                    session_date=session_date,
                    kind="close",
                    recipient=to,
                    sender=frm,
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise
        print(f"scheduler {session_date}: {result.status} → {to} (msg {result.provider_msg_id}).")
        ping_success(hc)
        return "sent"
    except Exception as exc:
        ping_fail(hc, f"{session_date}: {exc!r}")
        raise


_PRIOR_TAPE = text("""
    SELECT symbol, last FROM quotes
    WHERE session_date = :prior_session AND symbol = ANY(:symbols)
      AND last IS NOT NULL
""")


def _prior_tape_levels(
    conn: Connection, symbols: list[str], prior_session: date
) -> dict[str, Decimal]:
    """Yesterday's capture is today's base for the macro tape — nothing ingests
    futures or yield bars, so the tape bootstraps off its own history. Empty on
    the very first capture (there is no "yesterday" yet), which is exactly when
    `TAPE_SEED_LEVELS` (C2) supplies the base instead."""
    return {
        str(row["symbol"]): Decimal(str(row["last"]))
        for row in conn.execute(
            _PRIOR_TAPE, {"prior_session": prior_session, "symbols": symbols}
        ).mappings()
    }


def ingest_premarket_for_session(
    engine: Engine,
    *,
    session_date: date,
    prior_session: date,
    user_id: str = DEV_USER_ID,
    provider: PremarketProvider | None = None,
) -> int:
    """The 08:00 stage (docs/02): capture the morning's pre-market prints and the
    overnight macro tape into `quotes`.

    The provider defaults to the synthetic feed. That is not a test seam — it is
    the shipping configuration until the premium pre-market licence lands (D8),
    and swapping it is a one-line change here (see
    `constants.PREMARKET_FEED_IS_SYNTHETIC`, the one flag that flips alongside
    it).

    Held names and the tape are ingested in two calls with two disjoint closes
    dicts, never one (Change 2, M15 final-pass review): `TAPE_SEED_LEVELS`
    (C2) is scoped to `tape_closes` only. A held name gets its base from
    `bars_daily` alone — if that's empty (a brand-new holding, or a failed EOD
    ingest), the name has no base and `SyntheticPremarketProvider` omits it,
    per the standing rule that a symbol with no prior close is omitted, never
    invented (`worker/providers/synthetic.py`). Before this split, a single
    shared `closes` dict let a held name that happened to also be one of
    `TAPE_SEED_LEVELS`' five foreign-proxy ETFs (holding EWT directly, say)
    pick up the seed's invented level and emit a §3 gap — and a horizon-0
    claim — off a number nobody captured.
    """
    from worker.constants import TAPE_SEED_LEVELS
    from worker.premarket import (
        capture_stamp,
        ingest_premarket,
        prior_closes,
        tape_universe,
    )
    from worker.providers.synthetic import SyntheticPremarketProvider

    with engine.connect() as conn:
        held = book_symbols(conn, user_id)
        tape = tape_universe(sector_benchmarks(conn, user_id))
        tape_symbols = [symbol for symbol, _, _ in tape]

        # Held names: real prior closes only. No seed, ever — see the
        # docstring above.
        held_closes = prior_closes(conn, held, prior_session)

        # Tape: lowest priority first (C2, M15 review), the nominal seed, so
        # the tape always has *some* base on a cold start — nothing ingests
        # bars for futures/yield/forex series, so without this §2 can never
        # bootstrap. Each higher-priority source below overwrites it where
        # real data exists. Scoped to tape symbols only.
        tape_closes = dict(TAPE_SEED_LEVELS)
        tape_closes |= prior_closes(conn, tape_symbols, prior_session)

        # The tape's own prior capture, where one exists, wins over both the
        # seed and `bars_daily` (which never carries these symbols) — it's the
        # freshest real base once the tape has run at least once before.
        tape_closes |= _prior_tape_levels(conn, tape_symbols, prior_session)

    held_provider = provider or SyntheticPremarketProvider(held_closes, session_date)
    tape_provider = provider or SyntheticPremarketProvider(tape_closes, session_date)
    with engine.begin() as conn:
        stamp = capture_stamp(session_date)
        written = ingest_premarket(
            conn, held_provider,
            held=held, tape=[],
            session_date=session_date,
            captured_at=stamp,
        )
        written += ingest_premarket(
            conn, tape_provider,
            held=[], tape=tape,
            session_date=session_date,
            captured_at=stamp,
        )
        return written


def run_open_session_job(
    engine: Engine,
    *,
    now_utc: datetime,
    user_id: str = DEV_USER_ID,
    recipient: str | None = None,
    sender: str | None = None,
    healthcheck_url: str | None = None,
) -> str:
    """The 08:15 ET run. Returns ``skipped-holiday`` or ``sent``.

    In two respects it stays simpler than the close job: **no EOD bar poll**, because
    the open brief reads the *prior* close, which was already cached last night,
    and **no skip gate**, because the open brief always sends (docs/05). It does
    have ingest work of its own, though: the 08:00 stage (docs/02) —
    `ingest_premarket_for_session` captures the morning's pre-market prints and
    the overnight macro tape into `quotes` — before assembly reads them. M14
    shipped this job with no ingest work at all; M15 is that stage.
    """
    from worker.assemble_open import assemble_open_and_store
    from worker.deliver import deliver_brief
    from worker.narrate import default_narrator

    hc = config.HEALTHCHECKS_OPEN_URL if healthcheck_url is None else healthcheck_url
    session_date = calendar.today_et(now_utc)
    try:
        if not calendar.is_session(session_date):
            print(f"open {session_date}: not a trading session — skipping.")
            ping_success(hc)
            return "skipped-holiday"

        prior = calendar.previous_session(session_date)

        written = ingest_premarket_for_session(
            engine, session_date=session_date, prior_session=prior, user_id=user_id
        )
        print(f"open {session_date}: captured {written} pre-market quotes.")

        with engine.connect() as conn:
            trans = conn.begin()
            try:
                obj = assemble_open_and_store(
                    conn,
                    user_id,
                    session_date,
                    prior_session=prior,
                    generated_at=now_utc,
                    narrator=default_narrator(),
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise

        to = recipient or config.BRIEF_RECIPIENT
        frm = sender or config.BRIEF_FROM
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                result = deliver_brief(
                    conn,
                    user_id=user_id,
                    session_date=session_date,
                    kind="open",
                    recipient=to,
                    sender=frm,
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise
        print(f"open {session_date}: {result.status} → {to} ({obj.brief_id}).")
        ping_success(hc)
        return "sent"
    except Exception as exc:
        ping_fail(hc, f"{session_date} open: {exc!r}")
        raise


def _default_provider() -> MarketDataProvider:
    from worker.providers.tiingo import TiingoProvider

    return TiingoProvider()


# --- Attribution refit / PM-score / AM-reconcile jobs (M13) -----------------


def run_refit_job(
    engine: Engine,
    *,
    now_utc: datetime,
    model_version: int = ATTRIBUTION_MODEL_VERSION,
    healthcheck_url: str | None = None,
) -> str:
    """Weekend refit over every scored symbol, at the last completed session. Once
    the refit commits, surface the dashboard-only maintenance flags (Task 7,
    M13) — theme_misfit + beta_instability — for the dev user's held names."""
    from worker.attribution import refit
    from worker.maintenance import surface_maintenance_flags

    hc = config.HEALTHCHECKS_REFIT_URL if healthcheck_url is None else healthcheck_url
    try:
        fit_date = calendar.previous_session(calendar.today_et(now_utc) + timedelta(days=1))
        with engine.begin() as conn:
            refit(conn, fit_date, now_utc=now_utc, model_version=model_version)
        with engine.begin() as conn:
            surface_maintenance_flags(conn, DEV_USER_ID, fit_date, model_version)
        ping_success(hc)
        return "refit"
    except Exception as exc:
        ping_fail(hc, f"refit {now_utc:%Y-%m-%d}: {exc!r}")
        raise


def run_pm_score_job(
    engine: Engine,
    *,
    now_utc: datetime,
    model_version: int = ATTRIBUTION_MODEL_VERSION,
    healthcheck_url: str | None = None,
) -> str:
    """PM score (provisional/synthetic) for today's session."""
    from worker.attribution import score

    hc = config.HEALTHCHECKS_PM_URL if healthcheck_url is None else healthcheck_url
    session_date = calendar.today_et(now_utc)
    try:
        if not calendar.is_session(session_date):
            ping_success(hc)
            return "skipped-holiday"
        with engine.begin() as conn:
            score(
                conn,
                session_date,
                now_utc=now_utc,
                model_version=model_version,
                synthetic=True,
            )
        ping_success(hc)
        return "pm-scored"
    except Exception as exc:
        ping_fail(hc, f"pm-score {session_date}: {exc!r}")
        raise


def run_reconcile_job(
    engine: Engine,
    *,
    now_utc: datetime,
    model_version: int = ATTRIBUTION_MODEL_VERSION,
    healthcheck_url: str | None = None,
) -> str:
    """AM reconcile of the prior session's synthetic rows against the official bar."""
    from worker.reconcile import reconcile

    hc = config.HEALTHCHECKS_AM_URL if healthcheck_url is None else healthcheck_url
    today = calendar.today_et(now_utc)
    try:
        if not calendar.is_session(today):
            ping_success(hc)
            return "skipped-holiday"
        trade_date = calendar.previous_session(today)
        with engine.begin() as conn:
            reconcile(conn, trade_date, now_utc=now_utc, model_version=model_version)
        ping_success(hc)
        return "reconciled"
    except Exception as exc:
        ping_fail(hc, f"reconcile {today}: {exc!r}")
        raise


def _edgar_client() -> EdgarProvider:
    from worker.providers.edgar import EdgarClient

    return EdgarClient()


def _fundamentals_symbols(conn: Connection) -> list[str]:
    """Every symbol in every book. Fundamentals are shared reference data
    (docs/03), so this is per-symbol work, not per-user."""
    rows = conn.execute(text("SELECT DISTINCT symbol FROM holdings ORDER BY symbol"))
    return [str(s) for s in rows.scalars().all()]


def run_fundamentals_job(
    engine: Engine,
    *,
    now_utc: datetime,
    healthcheck_url: str | None = None,
) -> str:
    """Weekly EDGAR refresh (M18).

    Weekly, not daily: one `companyfacts` response carries a registrant's whole
    history — several MB — and 10-Qs land four times a year, so a daily poll
    would re-download megabytes to learn nothing.

    EDGAR needs no API key, but it does need a contact `User-Agent`. Unset, this
    **skips and still pings success**: a weekly fire that crash-loops would make
    a misconfigured optional feed look identical to a dead worker on the
    dead-man's switch, and would take the briefs down with it. The briefs do not
    depend on this job — stale fundamentals degrade two flags, they do not stop
    a send.
    """
    from worker import fundamentals

    hc = config.HEALTHCHECKS_FUNDAMENTALS_URL if healthcheck_url is None else healthcheck_url
    try:
        if not config.EDGAR_USER_AGENT:
            print("scheduler: EDGAR_USER_AGENT unset; skipping the fundamentals refresh")
            ping_success(hc)
            return "skipped-no-user-agent"

        client = _edgar_client()
        with engine.begin() as conn:
            symbols = _fundamentals_symbols(conn)
            if not symbols:
                ping_success(hc)
                return "skipped-empty-book"
            result = fundamentals.ingest_fundamentals(
                conn, client, symbols, as_of=calendar.today_et(now_utc)
            )
        if result["skipped"]:
            print(f"scheduler: fundamentals — no CIK at EDGAR for {result['skipped']}")
        ping_success(hc)
        return f"ingested-{result['stored']}"
    except Exception as exc:
        ping_fail(hc, f"fundamentals {calendar.today_et(now_utc)}: {exc!r}")
        raise


# --- The blocking loop ------------------------------------------------------


def run_scheduler(engine: Engine) -> None:  # pragma: no cover - exercised live, not in unit tests
    """Start the self-rescheduling loop. The brief job is a **single** one-shot
    rather than a cron or two competing triggers (D20's shape): each tick runs
    whichever kind is due (open or close), then asks ``next_kind_fire`` for the
    next one of either kind — keeping the half-day behaviour automatic for the
    close, the wall-clock behaviour exact for the open, and the ordering between
    them explicit. Alongside it, the M13 attribution fires each self-reschedule
    on their own one-shot and ping their own Healthchecks check: a weekly weekend
    refit, a PM synthetic score, and an AM reconcile."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.date import DateTrigger

    delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
    sched = BlockingScheduler(timezone="UTC")

    def tick(kind: str) -> None:
        try:
            now = datetime.now(UTC)
            if kind == "open":
                run_open_session_job(engine, now_utc=now)
            else:
                run_session_job(engine, now_utc=now)
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad day
            print(f"scheduler: {kind} run failed: {exc!r}")
        finally:
            nxt, nxt_kind = next_kind_fire(datetime.now(UTC) + timedelta(seconds=1), delay)
            sched.add_job(
                tick,
                DateTrigger(run_date=nxt),
                args=[nxt_kind],
                id="brief",
                replace_existing=True,
            )
            print(f"scheduler: next fire {nxt_kind} at {nxt.isoformat()}")

    def tick_refit() -> None:
        try:
            run_refit_job(engine, now_utc=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad fire
            print(f"scheduler: refit failed: {exc!r}")
        finally:
            nxt = next_weekly_fire(datetime.now(UTC) + timedelta(seconds=1), 5, clock_time(8, 0))
            sched.add_job(tick_refit, DateTrigger(run_date=nxt), id="refit", replace_existing=True)
            print(f"scheduler: next refit fire at {nxt.isoformat()}")

    def tick_fundamentals() -> None:
        try:
            run_fundamentals_job(engine, now_utc=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad fire
            print(f"scheduler: fundamentals failed: {exc!r}")
        finally:
            # Sunday 07:00 ET — off the weekend refit's slot and well clear of
            # any weekday send. Filings land on business days; nothing is lost
            # by refreshing on the quiet day.
            nxt = next_weekly_fire(datetime.now(UTC) + timedelta(seconds=1), 6, clock_time(7, 0))
            sched.add_job(
                tick_fundamentals, DateTrigger(run_date=nxt),
                id="fundamentals", replace_existing=True,
            )
            print(f"scheduler: next fundamentals fire at {nxt.isoformat()}")

    def tick_pm() -> None:
        try:
            run_pm_score_job(engine, now_utc=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad fire
            print(f"scheduler: pm-score failed: {exc!r}")
        finally:
            nxt = next_session_fire(datetime.now(UTC) + timedelta(seconds=1), clock_time(18, 30))
            sched.add_job(tick_pm, DateTrigger(run_date=nxt), id="pm", replace_existing=True)
            print(f"scheduler: next pm-score fire at {nxt.isoformat()}")

    def tick_am() -> None:
        try:
            run_reconcile_job(engine, now_utc=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad fire
            print(f"scheduler: reconcile failed: {exc!r}")
        finally:
            nxt = next_session_fire(datetime.now(UTC) + timedelta(seconds=1), clock_time(7, 0))
            sched.add_job(tick_am, DateTrigger(run_date=nxt), id="am", replace_existing=True)
            print(f"scheduler: next reconcile fire at {nxt.isoformat()}")

    first, first_kind = next_kind_fire(datetime.now(UTC), delay)
    sched.add_job(tick, DateTrigger(run_date=first), args=[first_kind], id="brief")
    print(f"scheduler: started; first fire {first_kind} at {first.isoformat()}")

    first_refit = next_weekly_fire(datetime.now(UTC), 5, clock_time(8, 0))
    sched.add_job(tick_refit, DateTrigger(run_date=first_refit), id="refit")
    print(f"scheduler: first refit fire at {first_refit.isoformat()}")

    first_fundamentals = next_weekly_fire(datetime.now(UTC), 6, clock_time(7, 0))
    sched.add_job(tick_fundamentals, DateTrigger(run_date=first_fundamentals), id="fundamentals")
    print(f"scheduler: first fundamentals fire at {first_fundamentals.isoformat()}")

    first_pm = next_session_fire(datetime.now(UTC), clock_time(18, 30))
    sched.add_job(tick_pm, DateTrigger(run_date=first_pm), id="pm")
    print(f"scheduler: first pm-score fire at {first_pm.isoformat()}")

    first_am = next_session_fire(datetime.now(UTC), clock_time(7, 0))
    sched.add_job(tick_am, DateTrigger(run_date=first_am), id="am")
    print(f"scheduler: first reconcile fire at {first_am.isoformat()}")

    sched.start()
