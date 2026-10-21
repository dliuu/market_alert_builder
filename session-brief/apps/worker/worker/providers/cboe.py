"""Cboe's delayed option chain and index history (M21).

The option chain behind cboe.com's own quote tables is served as one JSON
document per underlying — no key, 15 minutes delayed — with Cboe's bid/ask,
IV and greeks on every contract and Cboe's own IV30 on the underlying. The
VIX family is served as daily OHLC CSV files carrying full history. Neither
endpoint is documented, so this module fetches, keeps the verbatim text for
raw_payloads, and parses defensively: a missing field raises one of
FEED_ERRORS rather than producing a quiet wrong number.

Everything after the fetch is a pure function over the parsed payload, so the
same code runs on a saved file, in tests, and in the close job.

Units, verified on a live AAPL payload (2026-10-20):

- per-contract iv is a fraction (0.2604); the underlying iv30 is in
  vol points (26.831). Both are normalised to vol points here.
- iv of 0.0 is Cboe's "no value", emitted on deep in-the-money strikes.
- timestamp is UTC with no zone marker ("2026-10-20 23:08:56" beside
  last trades stamped 15:59:59 ET).
- current_price keeps moving in extended hours after the bell while
  close holds the 16:00 print, so moneyness is measured against close.
- open_interest and volume arrive as floats (439.0).
- the cdn.cboe.com host answers 307 to cdn-api.cboe.com; the client
  follows.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx

from worker.options import Contract, IndexBar, Underlying, parse_occ

CBOE_BASE_URL = "https://cdn-api.cboe.com/api/global"
USER_AGENT = "session-brief/0.1 (personal research; github.com/dliuu/market_alert_builder)"

# What ``raw_payloads`` keeps of a chain (D38): the underlying block and every
# option within this many days to expiry and this far from spot. A full
# large-cap chain is ~1.5 MB per session; nothing beyond these bounds is ever
# consulted downstream.
STORED_MAX_DTE = 60
STORED_MAX_MONEYNESS = Decimal("0.25")

# Everything a bad vendor response can raise — the FDN tuple, for the same
# reason: a quiet section beats a missing brief (see providers/fdn.py).
FEED_ERRORS = (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, ArithmeticError)

_HUNDRED = Decimal(100)


# --- transport ---------------------------------------------------------------


class CboeClient:
    """Thin transport over the Cboe endpoints. Every successful fetch is
    captured as ``(endpoint, symbol, verbatim text)`` so the caller can honour
    invariant 5; the client itself never touches the database."""

    def __init__(
        self,
        *,
        base_url: str = CBOE_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            transport=transport,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json, text/csv"},
        )
        self.captured: list[tuple[str, str, str]] = []

    def chain(self, symbol: str) -> dict[str, Any]:
        """The delayed option chain for one underlying, as parsed JSON with
        ``parse_float=Decimal``. Raises ``ValueError`` on an unexpected shape."""
        vendor_symbol = symbol.upper()
        text = self._get(f"delayed_quotes/options/{vendor_symbol}.json")
        payload = json.loads(text, parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError(f"cboe chain for {vendor_symbol}: unexpected top-level shape")
        if not isinstance(payload["data"].get("options"), list):
            raise ValueError(f"cboe chain for {vendor_symbol}: no options array")
        self.captured.append(("delayed_quotes/options", vendor_symbol, text))
        typed: dict[str, Any] = payload
        return typed

    def index_quote(self, symbol: str) -> dict[str, Any]:
        """The delayed quote document for a Cboe index (``VIX`` → ``_VIX.json``),
        the same shape as a stock chain. Its underlying ``close`` and
        ``current_price`` are today's index level; the history CSV lags that
        by a session on the evening of the session itself."""
        vendor_symbol = "_" + symbol.upper().lstrip("^_")
        text = self._get(f"delayed_quotes/options/{vendor_symbol}.json")
        payload = json.loads(text, parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError(f"cboe index quote for {vendor_symbol}: unexpected top-level shape")
        self.captured.append(("delayed_quotes/index", vendor_symbol, text))
        typed: dict[str, Any] = payload
        return typed

    def index_history(self, symbol: str) -> str:
        """Full daily OHLC history for a Cboe index (``VIX``, ``VIX9D``,
        ``VIX3M``) as CSV text. Raises ``ValueError`` if it isn't that file."""
        vendor_symbol = symbol.upper().lstrip("^")
        text = self._get(f"us_indices/daily_prices/{vendor_symbol}_History.csv")
        if not text.lstrip().upper().startswith("DATE,"):
            raise ValueError(f"cboe index history for {vendor_symbol}: not a DATE,... csv")
        self.captured.append(("us_indices/daily_prices", vendor_symbol, text))
        return text

    def _get(self, path: str) -> str:
        response = self._client.get(f"{self._base_url}/{path}")
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        """Release the underlying connection pool. The worker is a long-lived
        process that builds a fresh client every fire, so a client that is
        never closed leaks one pool per trading day."""
        self._client.close()


# --- parsing -----------------------------------------------------------------


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError(f"boolean where a number was expected: {value!r}")
    if isinstance(value, int | float | str):
        return Decimal(str(value))
    raise TypeError(f"not a number: {value!r}")


def _required(value: object, field: str) -> Decimal:
    out = _dec(value)
    if out is None:
        raise KeyError(f"cboe payload: missing {field}")
    return out


def _int(value: object) -> int:
    out = _dec(value)
    return int(out) if out is not None else 0


def _vol_points(value: object) -> Decimal | None:
    """Per-contract ``iv`` arrives as a fraction; 0 means none."""
    out = _dec(value)
    if out is None or out <= 0:
        return None
    return out * _HUNDRED


def parse_chain(payload: dict[str, Any]) -> tuple[Underlying, list[Contract]]:
    data = payload["data"]
    stamp = datetime.strptime(str(payload["timestamp"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    underlying = Underlying(
        symbol=str(data["symbol"]),
        close=_required(data.get("close"), "close"),
        current_price=_required(data.get("current_price"), "current_price"),
        prev_day_close=_dec(data.get("prev_day_close")),
        iv30=_dec(data.get("iv30")),
        timestamp=stamp,
    )
    contracts: list[Contract] = []
    for raw in data["options"]:
        occ = parse_occ(str(raw["option"]))
        contracts.append(
            Contract(
                symbol=str(raw["option"]),
                expiry=occ.expiry,
                right=occ.right,
                strike=occ.strike,
                bid=_required(raw.get("bid"), "bid"),
                ask=_required(raw.get("ask"), "ask"),
                bid_size=_int(raw.get("bid_size")),
                ask_size=_int(raw.get("ask_size")),
                iv=_vol_points(raw.get("iv")),
                delta=_dec(raw.get("delta")),
                gamma=_dec(raw.get("gamma")),
                theta=_dec(raw.get("theta")),
                vega=_dec(raw.get("vega")),
                rho=_dec(raw.get("rho")),
                open_interest=_int(raw.get("open_interest")),
                volume=_int(raw.get("volume")),
                last=_dec(raw.get("last_trade_price")),
                prev_day_close=_dec(raw.get("prev_day_close")),
                percent_change=_dec(raw.get("percent_change")),
            )
        )
    return underlying, contracts


def filter_payload_for_storage(
    payload: dict[str, Any],
    session_date: date,
    *,
    max_dte: int = STORED_MAX_DTE,
    max_moneyness: Decimal = STORED_MAX_MONEYNESS,
) -> dict[str, Any]:
    """The D38 subset: the underlying block as published, and only the option
    objects (as published, unmodified) expiring within ``max_dte`` days and
    struck within ``max_moneyness`` of the close. ``payload`` may be the plain
    ``json.loads`` of the response (the storage copy, numbers as floats) or the
    ``Decimal`` parse — the close is read through ``_dec`` either way."""
    data = payload["data"]
    spot = _required(data.get("close"), "close")
    kept: list[Any] = []
    for raw in data["options"]:
        occ = parse_occ(str(raw["option"]))
        if (occ.expiry - session_date).days > max_dte:
            continue
        if abs(occ.strike / spot - 1) > max_moneyness:
            continue
        kept.append(raw)
    return {
        "timestamp": payload["timestamp"],
        "data": {**{k: v for k, v in data.items() if k != "options"}, "options": kept},
    }


# --- index history -----------------------------------------------------------


def parse_index_csv(text: str) -> list[IndexBar]:
    """``DATE,OPEN,HIGH,LOW,CLOSE`` with ``MM/DD/YYYY`` dates, oldest first."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None or [f.strip().upper() for f in reader.fieldnames] != [
        "DATE",
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
    ]:
        raise ValueError(f"cboe index csv: unexpected header {reader.fieldnames!r}")
    bars: list[IndexBar] = []
    for row in reader:
        values = {k.strip().upper(): v for k, v in row.items() if k is not None}
        bars.append(
            IndexBar(
                session_date=datetime.strptime(values["DATE"].strip(), "%m/%d/%Y").date(),
                o=Decimal(values["OPEN"]),
                h=Decimal(values["HIGH"]),
                l=Decimal(values["LOW"]),
                c=Decimal(values["CLOSE"]),
            )
        )
    return bars
