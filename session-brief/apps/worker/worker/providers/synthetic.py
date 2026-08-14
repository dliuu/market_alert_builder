"""A deterministic pre-market feed, standing in for the licensed one (M15).

Pre-market quotes and the overnight macro tape are Premium-tier, redistribution-
gated data (docs/02, D8). Rather than block the sections on procurement, M15
builds them against this provider — the M7 `fundamentals` / M14 `events`
pattern — and swaps to `FdnProvider` once licensed. Both satisfy the same four
`PremarketProvider` methods, so nothing above the seam changes.

Determinism is the whole design constraint: the gap for a symbol is a pure
function of `(symbol, session_date)` via a hash, never `random`. That is what
makes a seeded morning snapshot-testable, and what makes "re-run the seed" a
no-op rather than a new story.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

# Gaps land in ±3.5%, wide enough that some names clear §3's 1% line and some
# don't — a seed where everything moves would never exercise the suppression.
_GAP_SPAN = Decimal("0.07")
_GAP_CENTER = Decimal("0.035")
# Pre-market volume, as a share of a nominal typical morning. The spread is what
# gives the volume multiple something to say.
_VOL_BASE = 40_000


class SyntheticPremarketProvider:
    """Deterministic pre-market and tape quotes derived from prior closes.

    ``prior_closes`` is read from ``bars_daily`` by the caller — a provider does
    not touch the database. A symbol with no prior close is omitted rather than
    invented: no base, no gap.
    """

    def __init__(self, prior_closes: dict[str, Decimal], session_date: date) -> None:
        self._closes = prior_closes
        self._session = session_date

    # --- the four seam methods --------------------------------------------

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            prev = self._closes.get(symbol)
            if prev is None:
                continue
            gap = self._unit(symbol, "gap") * _GAP_SPAN - _GAP_CENTER
            out.append({
                "symbol": symbol,
                "extended_last": (prev * (1 + gap)).quantize(Decimal("0.01")),
                "extended_v": int(_VOL_BASE * (Decimal("0.2") + self._unit(symbol, "vol") * 4)),
                "prev_close": prev,
            })
        return out

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    # --- internals ---------------------------------------------------------

    def _tape(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            prev = self._closes.get(symbol)
            if prev is None:
                continue
            move = self._unit(symbol, "tape") * Decimal("0.02") - Decimal("0.01")
            out.append({
                "symbol": symbol,
                "last": (prev * (1 + move)).quantize(Decimal("0.0001")),
                "prev_close": prev,
            })
        return out

    def _unit(self, symbol: str, salt: str) -> Decimal:
        """A stable value in [0, 1) for this symbol, this session, this axis."""
        digest = hashlib.sha256(
            f"{symbol}|{self._session.isoformat()}|{salt}".encode()
        ).digest()
        return Decimal(int.from_bytes(digest[:4], "big")) / Decimal(1 << 32)
