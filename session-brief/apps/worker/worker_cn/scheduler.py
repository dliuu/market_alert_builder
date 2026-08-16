"""CN-M2 — the two Chinese-book fires: the 09:10 CST open brief and the CN
close heartbeat (session close + ``CN_SEND_DELAY_MINUTES``).

Mirrors ``worker/scheduler.py``'s US jobs shape (injected ``now_utc``,
Healthchecks resolve-first, try/except -> ``ping_fail`` + raise) but never
generalizes the shared day-walk across markets (controller ruling, docs/08
CN-M2): each fire is computed inside XSHG's own calendar via
``worker.scheduler._wall_fire`` / ``CN.close_or_standard``, never
``worker.calendar``'s ET walk. ``worker.scheduler`` is imported as a module
(not individual names) so the two modules' mutual import — the one sanctioned
shared<->CN seam — resolves regardless of which one loads first; every
attribute access happens inside a function body, never at this module's own
top level.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as clock_time

from sqlalchemy.engine import Engine

from worker import scheduler as core_scheduler
from worker.constants import DEV_USER_ID
from worker.providers.base import MarketDataProvider
from worker_cn import config as cn_config
from worker_cn.calendar import CN
from worker_cn.config import cn_bars_are_synthetic
from worker_cn.constants import CN_BENCHMARK, CN_MARKET
from worker_cn.providers import SyntheticCnBarsProvider

SOURCE = "synthetic-cn"  # matches worker_cn/backfill.py's namespace


# --- Fire-time scheduling (pure) --------------------------------------------


def cn_open_fire_time(d: date) -> datetime | None:
    """When the CN open brief's fire for date ``d`` lands: 09:10 Asia/Shanghai,
    **XSHG sessions only**. ``None`` on a holiday — unlike the US open brief,
    the CN open brief has nothing to say on a day the market never opens
    (there is no prior-close-vs-tape read to make)."""
    if not CN.is_session(d):
        return None
    t = clock_time(cn_config.CN_OPEN_SEND_HOUR, cn_config.CN_OPEN_SEND_MINUTE)
    return core_scheduler._wall_fire(d, t, CN.tz)


def cn_close_fire_time(d: date) -> datetime:
    """When the CN close heartbeat for date ``d`` fires: the real session close
    (or the nominal 15:00 CST bell on a non-session day) plus
    ``CN_SEND_DELAY_MINUTES``. Fires **every calendar day** — the CN dead-man
    heartbeat, mirroring the US close's D20 semantics."""
    delay = timedelta(minutes=cn_config.CN_SEND_DELAY_MINUTES)
    return CN.close_or_standard(d) + delay


def next_cn_open_fire(now_utc: datetime) -> datetime:
    """The next CN open fire strictly after ``now_utc``, walked in XSHG's own
    calendar (``CN.local_date``) — never the shared ET day-walk."""
    d = CN.local_date(now_utc)
    for _ in range(8):  # today + a long holiday stretch (Golden Week) is under 8
        ft = cn_open_fire_time(d)
        if ft is not None and ft > now_utc:
            return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no CN open fire within 8 days of {now_utc}")  # pragma: no cover


def next_cn_close_fire(now_utc: datetime) -> datetime:
    """The next CN close heartbeat strictly after ``now_utc``, walked in
    XSHG's own calendar."""
    d = CN.local_date(now_utc)
    for _ in range(8):
        ft = cn_close_fire_time(d)
        if ft > now_utc:
            return ft
        d = d + timedelta(days=1)
    raise RuntimeError(f"no CN close fire within 8 days of {now_utc}")  # pragma: no cover


# --- The daily jobs ----------------------------------------------------------


def _default_cn_provider() -> MarketDataProvider:
    """The provider seam CN-M3 (Task 10) swaps a live vendor into. Only the
    synthetic branch exists today."""
    if cn_bars_are_synthetic():
        return SyntheticCnBarsProvider()
    raise RuntimeError("CN live bars land in CN-M3; run tiingo-cn-probe first")


def run_cn_close_session_job(
    engine: Engine,
    *,
    now_utc: datetime,
    user_id: str = DEV_USER_ID,
    recipient: str | None = None,
    sender: str | None = None,
    provider: MarketDataProvider | None = None,
    healthcheck_url: str | None = None,
) -> str:
    """The CN close heartbeat. Returns ``skipped-holiday``, ``skipped-quiet``
    (nothing moved >1%), or ``sent``. Pings its own Healthchecks check
    (``HEALTHCHECKS_CN_CLOSE_URL``) on the way out; a crash pings ``/fail`` and
    re-raises — never a stale send."""
    from worker.deliver import deliver_brief
    from worker_cn.assemble import assemble_cn_close_and_store

    hc = cn_config.HEALTHCHECKS_CN_CLOSE_URL if healthcheck_url is None else healthcheck_url
    session_date = CN.local_date(now_utc)
    try:
        if not CN.is_session(session_date):
            print(f"cn close {session_date}: not a trading session — skipping.")
            core_scheduler.ping_success(hc)
            return "skipped-holiday"

        with engine.connect() as conn:
            symbols = core_scheduler.book_symbols(
                conn, user_id, market=CN_MARKET, benchmark=CN_BENCHMARK
            )

        prov = provider or _default_cn_provider()
        core_scheduler.ensure_todays_bars(
            engine,
            prov,
            symbols,
            session_date,
            timeout_s=cn_config.CN_BAR_POLL_TIMEOUT_S,
            interval_s=cn_config.CN_BAR_POLL_INTERVAL_S,
            source=SOURCE,
        )

        # assemble_cn_close_and_store computes the book (compute_and_store,
        # market="CN") internally, same as the US path's assemble_and_store.
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                obj = assemble_cn_close_and_store(conn, user_id, session_date)
                trans.commit()
            except Exception:
                trans.rollback()
                raise

        if obj is None:
            print(f"cn close {session_date}: quiet session — nothing moved >1%, no send.")
            core_scheduler.ping_success(hc)
            return "skipped-quiet"

        from worker import config

        to = recipient or config.BRIEF_RECIPIENT
        frm = sender or config.BRIEF_FROM
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                result = deliver_brief(
                    conn,
                    user_id=user_id,
                    session_date=session_date,
                    kind="close_cn",
                    recipient=to,
                    sender=frm,
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise
        print(f"cn close {session_date}: {result.status} -> {to} (msg {result.provider_msg_id}).")
        core_scheduler.ping_success(hc)
        return "sent"
    except Exception as exc:
        core_scheduler.ping_fail(hc, f"{session_date}: {exc!r}")
        raise


def run_cn_open_session_job(
    engine: Engine,
    *,
    now_utc: datetime,
    user_id: str = DEV_USER_ID,
    recipient: str | None = None,
    sender: str | None = None,
    healthcheck_url: str | None = None,
) -> str:
    """The 09:10 CST CN open run. Returns ``skipped-holiday`` or ``sent``. No
    bar poll — the open brief reads the prior US close, already cached by the
    nightly US backfill — and no skip gate, matching the US open brief
    (docs/05: the open brief always sends)."""
    from worker.deliver import deliver_brief
    from worker_cn.assemble import assemble_cn_open_and_store

    hc = cn_config.HEALTHCHECKS_CN_OPEN_URL if healthcheck_url is None else healthcheck_url
    session_date = CN.local_date(now_utc)
    try:
        if not CN.is_session(session_date):
            print(f"cn open {session_date}: not a trading session — skipping.")
            core_scheduler.ping_success(hc)
            return "skipped-holiday"

        prior = CN.previous_session(session_date)

        with engine.connect() as conn:
            trans = conn.begin()
            try:
                obj = assemble_cn_open_and_store(
                    conn,
                    user_id,
                    session_date,
                    prior_session=prior,
                    generated_at=now_utc,
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise

        from worker import config

        to = recipient or config.BRIEF_RECIPIENT
        frm = sender or config.BRIEF_FROM
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                result = deliver_brief(
                    conn,
                    user_id=user_id,
                    session_date=session_date,
                    kind="open_cn",
                    recipient=to,
                    sender=frm,
                )
                trans.commit()
            except Exception:
                trans.rollback()
                raise
        print(f"cn open {session_date}: {result.status} -> {to} ({obj.brief_id}).")
        core_scheduler.ping_success(hc)
        return "sent"
    except Exception as exc:
        core_scheduler.ping_fail(hc, f"{session_date} open: {exc!r}")
        raise
