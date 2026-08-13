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
# Minutes after the session close to send the close brief (docs/02: send 16:45,
# i.e. close + 45). Moves with half-days because it's added to the real close.
SEND_DELAY_MINUTES: int = int(os.environ.get("SEND_DELAY_MINUTES", "45"))
# How long to poll Tiingo for today's EOD bar before giving up, and how often.
# Tiingo may publish a few minutes after the bell; a bounded poll avoids a false
# alarm without letting a stale brief go out late (docs/06).
BAR_POLL_TIMEOUT_S: int = int(os.environ.get("BAR_POLL_TIMEOUT_S", "1200"))
BAR_POLL_INTERVAL_S: int = int(os.environ.get("BAR_POLL_INTERVAL_S", "90"))
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
