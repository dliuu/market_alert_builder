"""Shared constants."""

from decimal import Decimal

# Single-user dev tenant, seeded in Alembic migration 0002_book and mirrored by
# the web app. Replaced by Supabase Auth's auth.uid() when user #2 exists.
DEV_USER_ID = "00000000-0000-0000-0000-000000000001"

# Book benchmark for the vs-market line (BriefObject.book.vs_spy_bps).
BENCHMARK_SYMBOL = "SPY"

# Attribution model version, stamped on every attribution row so a spec change
# never rewrites history (M11/D21).
ATTRIBUTION_MODEL_VERSION = 1

# --- Open brief §2/§3 (M15) -------------------------------------------------

# The overnight macro tape (docs/05 §2), as (symbol, label, feed). `feed` picks
# the provider method, which is what keeps the licensing story honest: futures,
# index and forex are three different premium endpoints.
TAPE_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("ES=F", "ES futures", "futures"),
    ("NQ=F", "NQ futures", "futures"),
    ("^TNX", "10Y", "index"),
    ("DXY", "DXY", "forex"),
    ("^VIX", "VIX", "index"),
    ("CL=F", "WTI", "futures"),
)

# Symbols quoted as a level, not a price: a percent change on a yield or on the
# VIX reads as noise ("the 10Y rose 0.7%"), while the absolute move is the
# figure a reader acts on ("+3bp"). §2 emits `overnight_pct = None` for these.
LEVEL_QUOTED: frozenset[str] = frozenset({"^TNX", "^VIX"})

# Foreign proxies, keyed by the *sector benchmark* the book already stores. This
# is what makes §2 relevant rather than generic (the spec: "chosen from the
# book's sectors") — a book with no semis sleeve gets no Taiwan line.
FOREIGN_PROXIES: dict[str, tuple[str, str]] = {
    "SMH": ("EWT", "Taiwan (semis)"),
    "XLK": ("EWJ", "Japan (tech)"),
    "XLE": ("EWC", "Canada (energy)"),
    "XLF": ("EUFN", "Europe (financials)"),
    "XLI": ("EWG", "Germany (industrials)"),
}

# §3's line: only names moving more than this pre-market get a row (docs/05).
PREMARKET_THRESHOLD: Decimal = Decimal("0.01")
