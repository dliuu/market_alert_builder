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
