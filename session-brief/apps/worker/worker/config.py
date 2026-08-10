"""Runtime configuration, read from the environment."""

import os

from dotenv import load_dotenv

load_dotenv()

# Supabase Postgres. Empty until .env is filled in — see docs/09-supabase-setup.md.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
