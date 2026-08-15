"""Runtime configuration, read from the environment."""

import os

from dotenv import load_dotenv

load_dotenv()

# Supabase Postgres. Empty until .env is filled in — see docs/09-supabase-setup.md.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# Tiingo daily EOD provider (D12). Empty until .env is filled in.
TIINGO_API_KEY: str = os.environ.get("TIINGO_API_KEY", "")

# Claude narration (stage ⑤, M8). Empty key ⇒ narration is skipped and the brief
# ships tables-only — the stage is deliberately non-fatal (docs/02, docs/04).
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
NARRATION_MODEL: str = os.environ.get("NARRATION_MODEL", "claude-opus-4-8")

# Delivery (stage ⑦, M9). The worker renders via the web app and sends via Resend.
# RESEND_API_KEY empty ⇒ `send` refuses rather than silently no-op'ing.
RESEND_API_KEY: str = os.environ.get("RESEND_API_KEY", "")
# Shared secret for the worker → web POST /api/render/:brief_id call (docs/02 #4).
RENDER_SHARED_SECRET: str = os.environ.get("RENDER_SHARED_SECRET", "")
# Base URL of the web app hosting the render endpoint (no trailing slash).
WEB_RENDER_URL: str = os.environ.get("WEB_RENDER_URL", "http://localhost:3000")
# From header, e.g. "Session Brief <brief@brief.yourdomain.com>".
BRIEF_FROM: str = os.environ.get("BRIEF_FROM", "")
# Default recipient for the `send` CLI command until per-user delivery exists.
BRIEF_RECIPIENT: str = os.environ.get("BRIEF_RECIPIENT", "")

# Scheduler + dead-man's switch (stage ①→⑦ orchestration, M10).
# Healthchecks.io ping URL. The scheduler GETs it on every successful run
# (including a correctly-skipped holiday) and GETs `<url>/fail` on a crash — so a
# dead worker stops pinging and the check goes red (docs/02). Empty ⇒ no ping.
HEALTHCHECKS_URL: str = os.environ.get("HEALTHCHECKS_URL", "")
# Per-fire dead-man's-switch checks for the attribution schedule (M13). Each
# attribution fire pings its own check, so a missed refit or a stuck reconcile is
# visible independently of the daily close heartbeat. Empty ⇒ no ping.
HEALTHCHECKS_REFIT_URL: str = os.environ.get("HEALTHCHECKS_REFIT_URL", "")
HEALTHCHECKS_PM_URL: str = os.environ.get("HEALTHCHECKS_PM_URL", "")
HEALTHCHECKS_AM_URL: str = os.environ.get("HEALTHCHECKS_AM_URL", "")
# Minutes after the session close to send the close brief (docs/02: send 16:45,
# i.e. close + 45). Moves with half-days because it's added to the real close.
SEND_DELAY_MINUTES: int = int(os.environ.get("SEND_DELAY_MINUTES", "45"))
# How long to poll Tiingo for today's EOD bar before giving up, and how often.
# Tiingo may publish a few minutes after the bell; a bounded poll avoids a false
# alarm without letting a stale brief go out late (docs/06).
BAR_POLL_TIMEOUT_S: int = int(os.environ.get("BAR_POLL_TIMEOUT_S", "1200"))
BAR_POLL_INTERVAL_S: int = int(os.environ.get("BAR_POLL_INTERVAL_S", "90"))
# When the poll gives up, the close job defers and re-fires on this interval
# instead of crashing into compute. Tiingo's free-tier EOD bar for session D is
# not reliably published by close+45+poll (16:45→17:05 ET): on 2026-08-14 the
# bars for the whole book were absent at 17:05 and present by 19:05, and no
# stored payload has ever captured session D earlier than 23:29 ET the same day.
# A bounded in-job poll cannot span that gap; a re-fire can, without holding the
# fire open for six hours.
BAR_RETRY_INTERVAL_MINUTES: int = int(os.environ.get("BAR_RETRY_INTERVAL_MINUTES", "30"))
# The last ET wall-clock time a retry may fire, on the session's own date.
# **This must stay before midnight ET.** `run_session_job` derives its session
# from `calendar.today_et(now)`, so a retry crossing midnight would compute the
# next day's session against today's book — a silent wrong-day brief, which is
# worse than the missed send it was trying to rescue. `retry_fire_time` enforces
# it; the hour is validated as a `time()` so 24+ can't be configured at all.
BAR_RETRY_UNTIL_ET_HOUR: int = int(os.environ.get("BAR_RETRY_UNTIL_ET_HOUR", "23"))
BAR_RETRY_UNTIL_ET_MINUTE: int = int(os.environ.get("BAR_RETRY_UNTIL_ET_MINUTE", "45"))
# Attribution PM/AM reconcile (M11): correct the stored return when the
# synthetic PM day-return and the official day-return differ by more than this
# (fraction; 0.001 = 10 bps). Silent drift would slowly corrupt every fitted β.
RECONCILE_TOL: float = float(os.environ.get("RECONCILE_TOL", "0.001"))

# Open brief (M14). A fixed **wall-clock** ET send, unlike the close brief's
# close-plus-delay: 08:15 is 08:15 whether or not it's a half-day (docs/02
# stages the morning as ingest 08:00 → assemble 08:10 → send 08:15).
OPEN_SEND_ET_HOUR: int = int(os.environ.get("OPEN_SEND_ET_HOUR", "8"))
OPEN_SEND_ET_MINUTE: int = int(os.environ.get("OPEN_SEND_ET_MINUTE", "15"))
# The open fire pings its own Healthchecks check — one dead-man's switch per
# scheduled run (docs/02), so a silent morning is distinguishable from a silent
# evening. Empty ⇒ no ping.
HEALTHCHECKS_OPEN_URL: str = os.environ.get("HEALTHCHECKS_OPEN_URL", "")

# The pre-market capture (M15). docs/02 stages the morning ingest at 08:00 ET;
# the capture stamp is what §3's header renders ("pre-market · 08:12 ET"), so it
# is configurable rather than derived from whenever the job happened to run.
PREMARKET_CAPTURE_ET_HOUR: int = int(os.environ.get("PREMARKET_CAPTURE_ET_HOUR", "8"))
PREMARKET_CAPTURE_ET_MINUTE: int = int(os.environ.get("PREMARKET_CAPTURE_ET_MINUTE", "12"))
# How many prior sessions of pre-market volume the §3 multiple averages, and the
# minimum it needs before it will report one. Short by design: pre-market volume
# regime-shifts with the news cycle, so a 30-day base would smear it.
PREMARKET_VOL_WINDOW: int = int(os.environ.get("PREMARKET_VOL_WINDOW", "10"))
PREMARKET_VOL_MIN_OBS: int = int(os.environ.get("PREMARKET_VOL_MIN_OBS", "5"))

# FinancialData.net (M16). One key is also the live/synthetic switch: empty ⇒
# the open brief's §2/§3/§4 run on the deterministic synthetic feed exactly as
# M14/M15 shipped them; set ⇒ FdnClient serves live pre-market, calendar, and
# news data. Premium tier ($69/mo, personal use) covers every endpoint we call.
FDN_API_KEY: str = os.environ.get("FDN_API_KEY", "")


def premarket_feed_is_synthetic() -> bool:
    """Whether §2/§3 are running on invented levels. Derived from the key, never
    hand-flipped — the constants.py flag this replaces (M15) rotted the moment
    it and the provider construction could disagree. `assemble_open` reads this
    to stamp `overnight_tape.synthetic` into `data_quality.stale`, which is the
    single source both renderers key their banner off."""
    return not FDN_API_KEY
