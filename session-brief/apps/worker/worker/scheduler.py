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
  you learn about Thursday's failure on Thursday, not Monday (docs/02). A close
  whose bars aren't published yet is a third state: it pings ``/log``, keeps the
  status where it is, and re-fires until its same-day deadline (D20, amended).

The pure parts (``fire_time``/``next_fire``) and the job body (``run_session_job``,
which takes an injected ``now``) are unit-tested without a clock or a network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time  # `time` is already the stdlib module here
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from worker import calendar, config
from worker.constants import ATTRIBUTION_MODEL_VERSION, BENCHMARK_SYMBOL, DEV_USER_ID
from worker.providers.base import MarketDataProvider, PremarketProvider
from worker_cn import scheduler as cn_scheduler

if TYPE_CHECKING:
    from worker.providers.fdn import FdnClient

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


def ping_log(url: str, detail: str) -> None:
    """Record an event **without moving the check's status.**

    The third state the switch needs: a deferral is neither a success (nothing
    was sent) nor a failure (a retry is pending and the vendor is merely late).
    Pinging ``/fail`` for it would train you to ignore a red check on the one
    signal that should always mean "look now". The run's own grace period still
    catches a deferral chain that never resolves, because a deferral never pings
    success — so silence still goes red on schedule.
    """
    if not url:
        return
    try:
        httpx.post(f"{url}/log", content=detail.encode("utf-8"), timeout=10.0)
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


def _wall_fire(d: date, t: clock_time, tz: ZoneInfo) -> datetime:
    """A wall-clock time ``t`` on date ``d`` in timezone ``tz``, converted to
    UTC. The general form ``_et_fire`` delegates to — worker_cn's own open fire
    (09:10 Asia/Shanghai) needs the same construction against a different tz."""
    return datetime.combine(d, t, tzinfo=tz).astimezone(UTC)


def _et_fire(d: date, at_et: clock_time) -> datetime:
    return _wall_fire(d, at_et, ET)


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


def open_fire_time(d: date) -> datetime:
    """When the open brief's fire for date ``d`` lands: a **fixed 08:15 ET**,
    every calendar day.

    This is deliberately *not* the close fire's construction. The close is
    anchored on the real session close plus a delay, which is what makes a
    half-day move the send automatically (D20). The open brief has no such
    anchor — it is read before the bell, and a half-day is still a normal
    morning — so it is a wall-clock time converted through ``America/New_York``
    (invariant 8, so DST doesn't shift it twice a year).

    It fires on non-sessions too, matching ``close_or_standard``'s nominal-bell
    pattern — this used to return ``None`` on a non-session day, so
    ``next_kind_fire`` never produced an "open" candidate at all on a weekend
    or holiday. ``run_open_session_job`` already checks ``is_session`` itself
    and pings success before doing any other work, so the only thing missing
    was ever *reaching* that check: with no candidate, the job was simply never
    invoked, and ``HEALTHCHECKS_OPEN_URL`` went dark for the entire weekend —
    every weekend — with no failure and no fire to explain it. Exactly the
    "weekday-only cron reads a holiday as a failed check-in" anti-pattern D20
    built the close job to avoid, just not applied here until now.
    """
    et_time = clock_time(config.OPEN_SEND_ET_HOUR, config.OPEN_SEND_ET_MINUTE)
    return datetime.combine(d, et_time, tzinfo=ET).astimezone(UTC)


def retry_deadline(session_date: date) -> datetime:
    """The last instant a close retry for ``session_date`` may fire (ET → UTC)."""
    cutoff = clock_time(config.BAR_RETRY_UNTIL_ET_HOUR, config.BAR_RETRY_UNTIL_ET_MINUTE)
    return _et_fire(session_date, cutoff)


def retry_fire_time(
    now_utc: datetime,
    session_date: date,
    interval: timedelta | None = None,
) -> datetime | None:
    """When to re-attempt ``session_date``'s close, or ``None`` to give up.

    Bounded by ``retry_deadline`` so the chain always terminates on the session's
    own ET date — see the invariant in ``config.BAR_RETRY_UNTIL_ET_HOUR``. A
    candidate landing past the deadline yields ``None`` rather than being clamped
    *to* the deadline, which would otherwise busy-retry against the cutoff.
    """
    step = interval or timedelta(minutes=config.BAR_RETRY_INTERVAL_MINUTES)
    candidate = now_utc + step
    return candidate if candidate <= retry_deadline(session_date) else None


def next_after_outcome(
    now_utc: datetime,
    outcome: str,
    session_date: date,
    delay: timedelta,
) -> tuple[datetime, str]:
    """Where the self-rescheduling one-shot points after a run of ``outcome``.

    Every outcome but a deferral resumes the ordinary open/close rotation. A
    deferral inserts one close retry ahead of it — but never *past* the next
    scheduled fire: the scheduler keeps a single ``brief`` job id (D20), so a
    retry placed after the next open would silently replace it and cost a
    morning brief. Deferring a close must never cost a different send.
    """
    if outcome == "deferred-no-bars":
        retry_at = retry_fire_time(now_utc, session_date)
        regular = next_kind_fire(now_utc, delay)
        if retry_at is not None and retry_at < regular[0]:
            return retry_at, "close"
        return regular
    return next_kind_fire(now_utc, delay)


def next_open_fire(now_utc: datetime) -> datetime:
    """The next open-brief wall-clock fire strictly after ``now_utc``. Its own
    day-walk (mirroring ``next_fire``'s shape) since ``open_fire_time`` now
    fires every calendar day — no session gate at this layer, matching
    ``next_fire``'s own heartbeat-on-holidays behaviour."""
    d = calendar.today_et(now_utc)
    for _ in range(8):
        ft = open_fire_time(d)
        if ft > now_utc:
            return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no open fire within 8 days of {now_utc}")  # pragma: no cover


def next_kind_fire(now_utc: datetime, delay: timedelta) -> tuple[datetime, str]:
    """The next fire of *any* of the four kinds, as ``(instant, kind)``.

    Min over four independent candidate streams — the US close walk
    (``next_fire``), the US open walk (``next_open_fire``), and CN's own two
    walks (``worker_cn.scheduler.next_cn_open_fire`` / ``next_cn_close_fire``,
    the one sanctioned shared→CN seam). Each stream is computed entirely
    within its own market's calendar rather than one day-walk generalized
    across markets (controller ruling): a shared "today" is wrong across
    markets — Shanghai's Monday 09:10 is Sunday evening ET. Tie-break stays
    deterministic: ``sorted`` compares the ``(datetime, kind)`` tuples.
    """
    candidates: list[tuple[datetime, str]] = [
        (next_fire(now_utc, delay), "close"),
        (next_open_fire(now_utc), "open"),
        (cn_scheduler.next_cn_open_fire(now_utc), "open_cn"),
        (cn_scheduler.next_cn_close_fire(now_utc), "close_cn"),
    ]
    return sorted(candidates)[0]


# --- The daily job ----------------------------------------------------------


def sector_benchmarks(conn: Connection, user_id: str, *, market: str = "US") -> list[str]:
    """Every benchmark set in the book, for the given market's sectors. Settable
    in the UI since M1, and read by two callers: the ingest universe and §2's
    foreign proxies."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT benchmark_symbol FROM sectors "
            "WHERE user_id = :u AND market = :market AND benchmark_symbol IS NOT NULL"
        ),
        {"u": user_id, "market": market},
    ).all()
    return sorted(str(r[0]) for r in rows)


def theme_symbols(conn: Connection) -> list[str]:
    """Every symbol in a theme basket. Shared reference data, so no ``user_id``
    (D21) — the theme model is a property of the market, not of a book."""
    rows = conn.execute(text("SELECT DISTINCT symbol FROM theme_members")).all()
    return sorted(str(r[0]) for r in rows)


def held_symbols(
    conn: Connection, user_id: str = DEV_USER_ID, *, market: str = "US"
) -> set[str]:
    """The held names in one market's book (``sectors.market`` scopes the read —
    the US and CN books never blend)."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT h.symbol FROM holdings h "
            "JOIN sectors s ON s.id = h.sector_id AND s.user_id = h.user_id "
            "WHERE h.user_id = :u AND s.market = :market"
        ),
        {"u": user_id, "market": market},
    ).all()
    return {str(r[0]) for r in rows}


def book_symbols(
    conn: Connection,
    user_id: str = DEV_USER_ID,
    *,
    market: str = "US",
    benchmark: str | None = None,
) -> list[str]:
    """The *book's* universe: every held name, every sector benchmark, plus the
    market benchmark. SPY is *always* needed for the vs-SPY line even when it
    isn't in the book — the manual ``backfill`` omits it, which is a latent gap
    the unattended run can't afford. The sector benchmarks are the same trap one
    level down: settable in the book UI since M1, never ingested, so the open
    brief's §5 trailing-5d line had nothing to read (M14).

    This is the pre-market capture's universe too, which is why theme members are
    *not* here — nobody wants a pre-market quote for a basket constituent they
    don't own. See ``ingest_symbols`` for the daily-bar universe."""
    benchmark_symbol = benchmark if benchmark is not None else BENCHMARK_SYMBOL
    held = held_symbols(conn, user_id, market=market)
    return sorted(
        held | set(sector_benchmarks(conn, user_id, market=market)) | {benchmark_symbol}
    )


def ingest_symbols(conn: Connection, user_id: str = DEV_USER_ID) -> list[str]:
    """Everything the daily-bar ingest must fetch: the book's universe plus the
    attribution model's theme constituents.

    Theme members were seeded as reference data but never ingested, so the weekly
    refit ran against symbols with no bars at all — 5 of 8 members on 2026-08-14
    — and its baskets could not be built. The model's own inputs have to be in
    the refresh set or it silently re-breaks every time a theme gains a name."""
    return sorted(set(book_symbols(conn, user_id)) | set(theme_symbols(conn)))


def required_symbols(conn: Connection, user_id: str = DEV_USER_ID) -> list[str]:
    """The symbols the close brief genuinely cannot go out without: the held
    names and the market benchmark.

    Deliberately narrower than ``ingest_symbols``. A theme constituent or a
    sector benchmark that a vendor publishes late has no bearing on tonight's
    P&L, and gating the send on it would hold back — and eventually fail — a
    brief whose every number was already computable. Fetch broadly; block on
    little."""
    return sorted(held_symbols(conn, user_id) | {BENCHMARK_SYMBOL})


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
    required: list[str] | None = None,
    timeout_s: int,
    interval_s: int,
    source: str | None = None,
    now_monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """Poll a provider until every *required* symbol has a bar for
    ``session_date`` or the timeout elapses. Ingest is idempotent (D13), so
    re-fetching a short window is cheap. Returns the set still missing at the
    end — empty means all present.

    ``symbols`` is what gets fetched; ``required`` is what the caller is willing
    to wait for, defaulting to all of ``symbols``. Keeping them separate is what
    lets the daily ingest cover the attribution model's theme constituents
    without letting a late basket member hold up a brief that does not read it.

    ``source`` defaults to Tiingo's namespace; the CN close job passes
    ``source="synthetic-cn"`` so its rows stay source-scoped, same as the
    backfill path (``worker_cn/backfill.py``)."""
    from worker.ingest import SOURCE as DEFAULT_SOURCE
    from worker.ingest import ingest_daily_bars
    from worker.normalize import normalize_bars

    src = source or DEFAULT_SOURCE
    gate = list(symbols) if required is None else required
    start = session_date - timedelta(days=7)  # small top-up window; history already stored
    deadline = now_monotonic() + timeout_s
    while True:
        with engine.begin() as conn:
            ingest_daily_bars(conn, provider, symbols, start, session_date, source=src)
            normalize_bars(conn, symbols, sources=(src,))
        missing = set(gate) - _bars_present(engine, gate, session_date)
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
    ``skipped-quiet`` (nothing moved >1%), ``deferred-no-bars``, ``failed-no-bars``,
    or ``sent``. Pings the dead-man's switch on the way out; a crash pings
    ``/fail`` and re-raises."""
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
            symbols = ingest_symbols(conn, user_id)
            required = required_symbols(conn, user_id)
        if poll:
            prov = provider or _default_provider()
            missing = ensure_todays_bars(
                engine,
                prov,
                symbols,
                session_date,
                required=required,
                timeout_s=config.BAR_POLL_TIMEOUT_S,
                interval_s=config.BAR_POLL_INTERVAL_S,
            )
            if missing:
                # The vendor hasn't published yet. Compute would raise "no bars
                # for … run backfill" here — advice that is both misleading (the
                # poll *is* the backfill, and it just ran) and useless (a manual
                # backfill this instant fetches the same absent data). Defer to a
                # later fire instead of converting a known wait into a crash.
                names = ", ".join(sorted(missing))
                retry_at = retry_fire_time(now_utc, session_date)
                if retry_at is None:
                    detail = (
                        f"{session_date}: no bars for {names} and the retry deadline "
                        f"({retry_deadline(session_date).astimezone(ET):%H:%M %Z}) has passed"
                    )
                    print(f"scheduler {detail}")
                    ping_fail(hc, detail)
                    return "failed-no-bars"
                print(
                    f"scheduler {session_date}: bars still missing after poll ({names}); "
                    f"deferring to {retry_at.astimezone(ET):%H:%M %Z}"
                )
                ping_log(
                    hc,
                    f"{session_date}: waiting on bars for {names}; retry at {retry_at:%H:%M}Z",
                )
                return "deferred-no-bars"

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
    client: FdnClient | None = None,
) -> int:
    """The 08:00 stage (docs/02): capture the morning's pre-market prints and the
    overnight macro tape into `quotes`.

    Live when a `client` is passed or `config.FDN_API_KEY` is set (M16);
    synthetic otherwise. That flip is not a test seam — it is the shipping
    configuration until the premium pre-market licence lands (D8), and
    swapping it is a one-line change here (see
    `config.premarket_feed_is_synthetic()`, the one flag that flips alongside
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
    from worker.providers.fdn import FdnClient, FdnPremarketProvider, store_captured_payloads
    from worker.providers.synthetic import SyntheticPremarketProvider

    live_client = client or (FdnClient() if config.FDN_API_KEY else None)

    with engine.connect() as conn:
        held = book_symbols(conn, user_id)
        tape = tape_universe(sector_benchmarks(conn, user_id))
        tape_symbols = [symbol for symbol, _, _ in tape]

        # Held names: real prior closes only. No seed, ever — see the
        # docstring above.
        held_closes = prior_closes(conn, held, prior_session)

        # The tape's base is a synthetic-branch concern only: live tape rows
        # derive prev_close from the vendor itself, so building this dict on
        # the live branch cost two DB queries for a value that was thrown
        # away. Built where it is used.
        tape_closes: dict[str, Decimal] = {}
        if live_client is None:
            # Lowest priority first (C2, M15 review), the nominal seed, so
            # the tape always has *some* base on a cold start — nothing
            # ingests bars for futures/yield/forex series, so without this §2
            # can never bootstrap. Each higher-priority source below
            # overwrites it where real data exists. Scoped to tape symbols.
            tape_closes = dict(TAPE_SEED_LEVELS)
            tape_closes |= prior_closes(conn, tape_symbols, prior_session)

            # The tape's own prior capture, where one exists, wins over both
            # the seed and `bars_daily` (which never carries these symbols) —
            # it's the freshest real base once the tape has run before.
            tape_closes |= _prior_tape_levels(conn, tape_symbols, prior_session)

    if live_client is not None:
        # Live (M16): held prev_closes still come from bars_daily — the one
        # authoritative base — while tape rows derive theirs from the vendor,
        # so TAPE_SEED_LEVELS is never consulted on this branch.
        held_provider = provider or FdnPremarketProvider(live_client, held_closes, session_date)
        tape_provider = provider or FdnPremarketProvider(live_client, {}, session_date)
    else:
        held_provider = provider or SyntheticPremarketProvider(held_closes, session_date)
        tape_provider = provider or SyntheticPremarketProvider(tape_closes, session_date)

    # Close only a client we built. One passed in belongs to the caller — the
    # open job reuses a single client across pre-market, calendars and news —
    # and closing it here would break its next fetch.
    owned_client = live_client if client is None else None
    try:
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
            if live_client is not None:
                store_captured_payloads(conn, live_client, as_of=session_date)
            return written
    finally:
        if owned_client is not None:
            owned_client.close()


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

        from worker.providers.fdn import FdnClient

        client = FdnClient() if config.FDN_API_KEY else None
        news: dict[str, list[str]] = {}
        missing: list[str] = []
        try:
            written = ingest_premarket_for_session(
                engine, session_date=session_date, prior_session=prior,
                user_id=user_id, client=client,
            )
            print(f"open {session_date}: captured {written} pre-market quotes.")

            if client is not None:
                from worker.events_fdn import ingest_events_for_session
                from worker.news_fdn import fetch_held_news
                from worker.providers.fdn import store_captured_payloads

                n_events, failed_calendar = ingest_events_for_session(
                    engine, client, session_date=session_date, user_id=user_id
                )
                missing.extend(failed_calendar)
                with engine.connect() as conn:
                    held = set(book_symbols(conn, user_id))
                news = fetch_held_news(client, session_date=session_date, held=held)
                with engine.begin() as conn:
                    store_captured_payloads(conn, client, as_of=session_date)
                print(
                    f"open {session_date}: {n_events} calendar events, "
                    f"news for {sorted(news)}."
                )
        finally:
            # One client, one connection pool, and this process outlives every
            # fire (docs/02: long-running, no cold starts) — an unclosed client
            # leaks a pool per trading day. Every fdn fetch is done by here.
            if client is not None:
                client.close()

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
                    news=news,
                    missing=missing,
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


# --- The blocking loop ------------------------------------------------------


def run_scheduler(engine: Engine) -> None:  # pragma: no cover - exercised live, not in unit tests
    """Start the self-rescheduling loop. The brief job is a **single** one-shot
    rather than a cron or two competing triggers (D20's shape): each tick runs
    whichever kind is due (open or close), then asks ``next_after_outcome`` for
    the next one of either kind — keeping the half-day behaviour automatic for
    the close, the wall-clock behaviour exact for the open, and the ordering
    between them explicit. A close that deferred on missing bars re-fires ahead
    of the rotation until its same-day deadline. Alongside it, the M13
    attribution fires each self-reschedule on their own one-shot and ping their
    own Healthchecks check: a weekly weekend refit, a PM synthetic score, and an
    AM reconcile."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.date import DateTrigger

    delay = timedelta(minutes=config.SEND_DELAY_MINUTES)
    sched = BlockingScheduler(timezone="UTC")

    def tick(kind: str) -> None:
        outcome = "failed"
        try:
            now = datetime.now(UTC)
            if kind == "open":
                run_open_session_job(engine, now_utc=now)
            elif kind == "close":
                outcome = run_session_job(engine, now_utc=now)
            elif kind == "open_cn":
                cn_scheduler.run_cn_open_session_job(engine, now_utc=now)
            else:  # close_cn
                cn_scheduler.run_cn_close_session_job(engine, now_utc=now)
        except Exception as exc:  # noqa: BLE001 — logged; the loop must survive a bad day
            print(f"scheduler: {kind} run failed: {exc!r}")
        finally:
            after = datetime.now(UTC) + timedelta(seconds=1)
            nxt, nxt_kind = next_after_outcome(after, outcome, calendar.today_et(after), delay)
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

    first_pm = next_session_fire(datetime.now(UTC), clock_time(18, 30))
    sched.add_job(tick_pm, DateTrigger(run_date=first_pm), id="pm")
    print(f"scheduler: first pm-score fire at {first_pm.isoformat()}")

    first_am = next_session_fire(datetime.now(UTC), clock_time(7, 0))
    sched.add_job(tick_am, DateTrigger(run_date=first_am), id="am")
    print(f"scheduler: first reconcile fire at {first_am.isoformat()}")

    sched.start()
