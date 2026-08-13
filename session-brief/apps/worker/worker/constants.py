"""Shared constants."""

# Single-user dev tenant, seeded in Alembic migration 0002_book and mirrored by
# the web app. Replaced by Supabase Auth's auth.uid() when user #2 exists.
DEV_USER_ID = "00000000-0000-0000-0000-000000000001"

# Book benchmark for the vs-market line (BriefObject.book.vs_spy_bps).
BENCHMARK_SYMBOL = "SPY"

# Attribution model version, stamped on every attribution row so a spec change
# never rewrites history (M11/D21).
ATTRIBUTION_MODEL_VERSION = 1
