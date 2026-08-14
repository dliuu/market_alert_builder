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

# Nominal seed levels for the tape universe (C2, M15 review): a base for the
# *first* capture of each symbol, before any real prior capture exists.
#
# §2's prior levels come from `bars_daily` (via `prior_closes`) and from the
# tape's own prior capture (via `_prior_tape_levels`) — but nothing ingests
# daily bars for futures, yield, or forex series (`book_symbols` is holdings ∪
# sector benchmarks ∪ SPY; Tiingo serves none of ES=F/NQ=F/^TNX/DXY/^VIX/CL=F
# or the foreign proxies), and the tape's own history is itself seeded from
# this same recursion — so with no seed the base is never established and §2
# is empty on every run, forever (confirmed empirically: 0 of the 7 tape
# symbols ever have a `bars_daily` row).
#
# These are plausible index/yield/price levels, not live data — nothing here is
# redistributed, and `ingest_premarket_for_session` treats this as the
# lowest-priority source, so a real prior capture (once one exists) always
# wins. This dict disappears the day a licensed pre-market/futures feed lands
# and `prior_closes`/`_prior_tape_levels` have real data to return.
TAPE_SEED_LEVELS: dict[str, Decimal] = {
    "ES=F": Decimal("5620.00"),   # E-mini S&P 500 futures, nominal index level
    "NQ=F": Decimal("19800.00"),  # E-mini Nasdaq-100 futures
    "^TNX": Decimal("4.20"),      # 10Y yield, around 4%
    "DXY": Decimal("103.00"),     # Dollar index
    "^VIX": Decimal("15.00"),     # VIX, in the teens
    "CL=F": Decimal("65.00"),     # WTI crude, in the sixties
    "EWT": Decimal("54.00"),      # Taiwan (semis) proxy
    "EWJ": Decimal("72.00"),      # Japan (tech) proxy
    "EWC": Decimal("44.00"),      # Canada (energy) proxy
    "EUFN": Decimal("26.00"),     # Europe (financials) proxy
    "EWG": Decimal("34.00"),      # Germany (industrials) proxy
}

# The one switch a licensed pre-market/futures feed flips (final-pass review,
# M15). While this is True, §2 (overnight tape) is running on
# `SyntheticPremarketProvider`, not live prints: `assemble_open` reads it to add
# `"overnight_tape.synthetic"` to `data_quality.stale`, and both renderers
# (`open-brief.tsx`, `briefs/[slug]/page.tsx`) key their "synthetic feed · not
# live prices" header marker off that one `data_quality.stale` entry — neither
# renderer reads this constant directly. The day `TAPE_SEED_LEVELS` above goes
# away and `ingest_premarket_for_session` starts constructing a live provider,
# flip this too. Nothing else needs to change: the marker comes off both
# renderers automatically once `data_quality.stale` stops carrying the entry.
PREMARKET_FEED_IS_SYNTHETIC: bool = True
