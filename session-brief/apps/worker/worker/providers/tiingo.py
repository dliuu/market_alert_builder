"""Tiingo daily EOD provider (D12). Free tier: personal, non-commercial.

Only ``daily_bars`` is implemented — Tiingo's free tier covers neither an
earnings calendar nor news, which are TBD source gaps for M7/M8.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from worker.config import TIINGO_API_KEY

_BASE_URL = "https://api.tiingo.com/tiingo/daily"


class TiingoProvider:
    def __init__(self, api_key: str | None = None, *, base_url: str = _BASE_URL) -> None:
        key = api_key if api_key is not None else TIINGO_API_KEY
        if not key:
            raise RuntimeError("TIINGO_API_KEY is not set (see repo-root .env)")
        self._key = key
        self._base_url = base_url

    def daily_bars(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        # Key travels in the Authorization header, never the URL (no secrets in
        # query strings). parse_float=Decimal keeps prices off the float path.
        response = httpx.get(
            f"{self._base_url}/{symbol.lower()}/prices",
            params={"startDate": start.isoformat(), "endDate": end.isoformat()},
            headers={"Authorization": f"Token {self._key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        data = json.loads(response.text, parse_float=Decimal)
        if not isinstance(data, list):
            raise ValueError(f"Tiingo returned non-list for {symbol}: {data!r}")
        return data

    def quote(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError("Tiingo quote endpoint is not wired until it's needed")

    def earnings_calendar(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("earnings calendar is a TBD source gap (M7)")

    def news(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        raise NotImplementedError("company news is a TBD source gap (M8)")
