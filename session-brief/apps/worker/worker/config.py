"""Runtime configuration, read from the environment."""

import os

from dotenv import load_dotenv

load_dotenv()

# Supabase Postgres. Empty until .env is filled in — see docs/09-supabase-setup.md.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# Tiingo daily EOD provider (D12). Empty until .env is filled in.
TIINGO_API_KEY: str = os.environ.get("TIINGO_API_KEY", "")
