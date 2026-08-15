"""A deterministic pre-market feed, the keyless fallback for the licensed one.

Pre-market quotes and the overnight macro tape are Premium-tier, redistribution-
gated data (docs/02, D8). Rather than block the sections on procurement, M15
built them against this provider — the M7 `fundamentals` / M14 `events`
pattern. M16 shipped the live half: with `FDN_API_KEY` set,
`ingest_premarket_for_session` constructs `FdnPremarketProvider` instead and
this file is not consulted. Both satisfy the same four `PremarketProvider`
methods, so nothing above the seam changes — which is why the switch is a
config value, not a code edit.

Determinism is the whole design constraint: the gap for a symbol is a pure
function of `(symbol, session_date)` via a hash, never `random`. That is what
makes a seeded morning snapshot-testable, and what makes "re-run the seed" a
no-op rather than a new story.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
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


# Names and titles the seed draws from. A cluster needs three distinct people,
# so the pool has to be wider than the cluster threshold or the rule could
# never fire on seeded data.
_INSIDERS: tuple[tuple[str, str | None], ...] = (
    ("Dana Whitfield", "Chief Executive Officer"),
    ("Marcus Iyer", "Chief Financial Officer"),
    ("Priya Raghunathan", "SVP Engineering"),
    ("Tomas Berger", "Director"),
    ("Lena Osei", "Chief Operating Officer"),
)
_CODES = ("S", "S", "S", "P", "F")


class SyntheticCatalystProvider:
    """A deterministic Form 4 / Form 144 feed, standing in for the licensed one.

    Same contract as ``SyntheticPremarketProvider`` (M15/D28): every value is a
    pure function of ``(symbol, session_date, axis)`` through a hash, never
    ``random``, so a seeded session is snapshot-testable and re-seeding is a
    no-op rather than a new story. Swaps for the live ``FdnProvider`` once M16's
    ``FdnClient`` lands — nothing above the seam changes.
    """

    def __init__(self, session_date: date) -> None:
        self._session = session_date

    def insider_transactions(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        count = 1 + int(self._unit(symbol, "n_insider") * 4)
        for i in range(count):
            name, title = _INSIDERS[(i + int(self._unit(symbol, "who") * 5)) % len(_INSIDERS)]
            price = (Decimal("20") + self._unit(symbol, f"px{i}") * 80).quantize(Decimal("0.01"))
            shares = Decimal(500 + int(self._unit(symbol, f"sh{i}") * 4500))
            on = self._session - timedelta(days=i)
            rows.append({
                "symbol": symbol,
                "insider_name": name,
                "insider_title": title,
                "transaction_date": on.isoformat(),
                "filing_date": on.isoformat(),
                "transaction_type": _CODES[int(self._unit(symbol, f"code{i}") * len(_CODES))],
                "shares": str(shares),
                "price": str(price),
                # The remaining holding has to *vary*: a fixed multiple would pin
                # pct_of_holding at one value and `outsized_sale` (>=40%) could
                # never fire on seeded data, leaving a whole rule undevelopable.
                # 0.4x-6x spans both sides of the threshold.
                "shares_after": str(
                    (shares * (Decimal("0.4") + self._unit(symbol, f"after{i}") * 6))
                    .quantize(Decimal("1"))
                ),
            })
        return rows[offset:]

    def proposed_sales(self, symbol: str, *, offset: int = 0) -> list[dict[str, Any]]:
        if self._unit(symbol, "has_144") < Decimal("0.5"):
            return []
        shares = Decimal(50_000 + int(self._unit(symbol, "144sh") * 900_000))
        # Age spans the 45-day conversion window in both directions, or
        # `unconverted_144` would never fire on seeded data.
        age = 1 + int(self._unit(symbol, "144age") * 90)
        return [{
            "symbol": symbol,
            "insider_name": _INSIDERS[int(self._unit(symbol, "144who") * 5)][0],
            "filing_date": (self._session - timedelta(days=age)).isoformat(),
            "shares_proposed": str(shares),
            "approx_sale_date": None,
            "broker": "Seeded Securities LLC",
        }][offset:]

    def public_float(self, symbol: str) -> Decimal | None:
        # Deliberately unknown for some symbols: "size unknown" is a rendering
        # path the section has to exercise (open question 4).
        if self._unit(symbol, "has_float") < Decimal("0.25"):
            return None
        return Decimal(20_000_000 + int(self._unit(symbol, "float") * 400_000_000))

    def _unit(self, symbol: str, salt: str) -> Decimal:
        digest = hashlib.sha256(
            f"{symbol}|{self._session.isoformat()}|{salt}".encode()
        ).digest()
        return Decimal(int.from_bytes(digest[:4], "big")) / Decimal(1 << 32)
