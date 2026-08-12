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
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from worker import calendar, config
from worker.constants import BENCHMARK_SYMBOL, DEV_USER_ID
from worker.providers.base import MarketDataProvider

UTC = ZoneInfo("UTC")


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
    """The next fire instant strictly after ``now_utc``. Walks forward from
    today's ET date until a day's fire time is in the future — so a run that
    finishes at/after its own fire time rolls cleanly to the next day."""
    d = calendar.today_et(now_utc)
    for _ in range(8):  # today + a long weekend/holiday stretch is well under 8
        ft = fire_time(d, delay)
        if ft > now_utc:
            return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no fire time within 8 days of {now_utc}")  # pragma: no cover


# --- The daily job ----------------------------------------------------------


def book_symbols(engine: Engine, user_id: str = DEV_USER_ID) -> list[str]:
    """The symbols to refresh: every held name plus the benchmark. The benchmark
    is *always* needed for the vs-SPY line even when it isn't in the book — the
    manual ``backfill`` omits it, which is a latent gap the unattended run can't
    afford."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT symbol FROM holdings WHERE user_id = :u ORDER BY symbol"),
            {"u": user_id},
        ).all()
    return sorted({str(r[0]) for r in rows} | {BENCHMARK_SYMBOL})


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

        symbols = book_symbols(engine, user_id)
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


def _default_provider() -> MarketDataProvider:
    from worker.providers.tiingo import TiingoProvider

    return TiingoProvider()


# --- The blocking loop ------------------------------------------------------


def run_scheduler(engine: Engine) -> None:  # pragma: no cover - exercised live, not in unit tests
    """Start the self-rescheduling daily loop. A single one-shot job fires at the
    next close+delay, runs the day's job, then schedules the following day —
    rather than a fixed cron, so the send tracks half-days automatically."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.date import DateTrigger

    delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
    sched = BlockingScheduler(timezone="UTC")

    def tick() -> None:
        try:
            run_session_job(engine, now_utc=datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad day
            print(f"scheduler: run failed: {exc!r}")
        finally:
            nxt = next_fire(datetime.now(UTC) + timedelta(seconds=1), delay)
            sched.add_job(tick, DateTrigger(run_date=nxt), id="daily", replace_existing=True)
            print(f"scheduler: next fire at {nxt.isoformat()}")

    first = next_fire(datetime.now(UTC), delay)
    sched.add_job(tick, DateTrigger(run_date=first), id="daily")
    print(f"scheduler: started; first fire at {first.isoformat()}")
    sched.start()
