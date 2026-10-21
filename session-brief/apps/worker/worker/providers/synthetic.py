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
from decimal import Decimal, localcontext
from typing import Any

from worker.calendar import previous_session

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
                "trading_symbol": symbol,
                "insider_name": name,
                "relationship_to_issuer": title,
                "transaction_date": on.isoformat(),
                "transaction_code": _CODES[int(self._unit(symbol, f"code{i}") * len(_CODES))],
                "amount_of_securities": str(shares),
                "price_per_security": str(price),
                "is_derivatives_transaction": False,
                # The remaining holding has to *vary*: a fixed multiple would pin
                # pct_of_holding at one value and `outsized_sale` (>=40%) could
                # never fire on seeded data, leaving a whole rule undevelopable.
                # 0.4x-6x spans both sides of the threshold.
                "securities_owned_following_transaction": str(
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
            "trading_symbol": symbol,
            "seller_name": _INSIDERS[int(self._unit(symbol, "144who") * 5)][0],
            "amount_of_securities_to_be_sold": str(shares),
            "approximate_date_of_sale": (self._session - timedelta(days=age)).isoformat(),
            "broker_name": "Seeded Securities LLC",
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


# --- Options (M21) -----------------------------------------------------------
#
# The keyless stand-in for Cboe's delayed chain. Like the pre-market feed above
# it is a pure function of its inputs (`closes`, `session_date`) via `hashlib`,
# never `random`, and Decimal-only: a re-run is byte-identical and nothing here
# touches the database. Payloads carry the keys and units `parse_chain` and
# `parse_index_csv` read from the live feed (per-contract `iv` a fraction,
# `iv30` in vol points).
#
# The time value is a Brenner-Subrahmanyam-style approximation, 0.4 * S * sigma
# * sqrt(T), damped by exp(-3 * |moneyness|), rather than Black-Scholes. It
# needs only sqrt and exp, which `Decimal` provides exactly in its own context,
# so no float (and no normal CDF) ever enters the feed; the shape (more value
# at the money, more with time, more with vol) is all the downstream maths
# needs from an invented chain.

_OPT_BASE_IV = Decimal("0.30")
_OPT_SMILE = Decimal("0.004")  # added per percentage point of |moneyness|
_OPT_TV_SCALE = Decimal("0.4")
_OPT_TV_DECAY = Decimal(3)
_OPT_SPREAD = Decimal("0.02")
_OPT_DELTA_SLOPE = Decimal(2)
_OPT_GREEK = Decimal("0.01")
_OPT_STRIKE_STEPS = range(-8, 9)  # 2.5% apart, -20%..+20%
_OPT_STRIKE_STEP = Decimal("0.025")
_CENT = Decimal("0.01")
_VIX_BASE = Decimal(18)
_VIX_OFFSETS = {"VIX": Decimal(0), "VIX9D": Decimal(-2), "VIX3M": Decimal(1)}
_HISTORY_DAYS = 300


def _vix_jitter(day: date) -> Decimal:
    """Deterministic in [-3.00, +3.00], to the cent, from the date alone."""
    digest = hashlib.sha256(f"VIX:{day}".encode()).digest()
    return Decimal(int.from_bytes(digest[:8], "big") % 601 - 300) / 100


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


class SyntheticOptionsProvider:
    """Satisfies `OptionsProvider` with an invented, deterministic chain."""

    def __init__(self, closes: dict[str, Decimal], session_date: date) -> None:
        self._closes = closes
        self._session = session_date

    def _expiries(self) -> list[date]:
        d = self._session
        first = d + timedelta(days=(4 - d.weekday()) % 7 or 7)
        found = {first + timedelta(weeks=i) for i in range(8)}
        for ahead in (1, 2, 3):
            index = d.year * 12 + d.month - 1 + ahead
            found.add(_third_friday(index // 12, index % 12 + 1))
        return sorted(found)

    def _contract(
        self, symbol: str, close: Decimal, expiry: date, right: str, strike: Decimal
    ) -> dict[str, Any]:
        moneyness = (strike - close) / close
        iv = _OPT_BASE_IV + _OPT_SMILE * abs(moneyness * 100)
        dte = Decimal((expiry - self._session).days)
        intrinsic = (
            max(close - strike, Decimal(0)) if right == "C" else max(strike - close, Decimal(0))
        )
        with localcontext() as ctx:
            ctx.prec = 28
            time_value = (
                _OPT_TV_SCALE
                * close
                * iv
                * (dte / 365).sqrt()
                * (-_OPT_TV_DECAY * abs(moneyness)).exp()
            )
        mid = intrinsic + time_value
        call_delta = min(
            max(Decimal("0.5") - _OPT_DELTA_SLOPE * moneyness, Decimal("0.02")), Decimal("0.98")
        )
        delta = call_delta if right == "C" else call_delta - 1
        occ = f"{symbol}{expiry:%y%m%d}{right}{int(strike * 1000):08d}"
        return {
            "option": occ,
            "bid": (mid * (1 - _OPT_SPREAD)).quantize(_CENT),
            "ask": (mid * (1 + _OPT_SPREAD)).quantize(_CENT),
            "bid_size": 10,
            "ask_size": 10,
            "iv": iv,
            "delta": delta,
            "gamma": _OPT_GREEK,
            "theta": _OPT_GREEK,
            "vega": _OPT_GREEK,
            "rho": _OPT_GREEK,
            "open_interest": 100,
            "volume": 10,
        }

    def chain(self, symbol: str) -> dict[str, Any]:
        close = self._closes[symbol]
        strikes = [(close * (1 + _OPT_STRIKE_STEP * n)).quantize(_CENT) for n in _OPT_STRIKE_STEPS]
        options = [
            self._contract(symbol, close, expiry, right, strike)
            for expiry in self._expiries()
            for strike in strikes
            for right in ("C", "P")
        ]
        return {
            "timestamp": f"{self._session} 21:00:00",
            "data": {
                "symbol": symbol,
                "close": close,
                "current_price": close,
                "prev_day_close": close,
                "iv30": Decimal("30.0"),
                "options": options,
            },
        }

    def _level(self, symbol: str, day: date) -> Decimal:
        return _VIX_BASE + _VIX_OFFSETS[symbol] + _vix_jitter(day)

    def index_quote(self, symbol: str) -> dict[str, Any]:
        level = self._level(symbol, self._session)
        return {
            "timestamp": f"{self._session} 21:00:00",
            "data": {
                "symbol": f"_{symbol}",
                "close": level,
                "current_price": level,
                "prev_day_close": level,
                "options": [],
            },
        }

    def index_history(self, symbol: str) -> str:
        last = previous_session(self._session)
        lines = ["DATE,OPEN,HIGH,LOW,CLOSE"]
        for back in range(_HISTORY_DAYS - 1, -1, -1):
            day = last - timedelta(days=back)
            level = self._level(symbol, day)
            lines.append(f"{day:%m/%d/%Y},{level},{level},{level},{level}")
        return "\n".join(lines) + "\n"
