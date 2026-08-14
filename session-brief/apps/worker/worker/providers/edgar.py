"""SEC EDGAR client (M18). Free, keyless, authoritative XBRL.

No API key exists for EDGAR and none is needed — but the SEC does impose two
conditions of use, and both are enforced here rather than left to good
intentions:

- **A `User-Agent` naming a real contact.** Requests without one get 403. It is
  configuration (`EDGAR_USER_AGENT`), and construction refuses without it, so
  the failure lands at startup rather than mid-run.
- **10 requests/second**, across all SEC hosts. A token bucket paces every call.

Three endpoints, because the data is split across them:

- `company_tickers.json` — the ticker → CIK map. One small file for every
  registrant, fetched once per process.
- `companyfacts/CIK##########.json` — numeric XBRL facts, the whole history in
  one response (several MB). One call per symbol, not per period.
- `submissions/CIK##########.json` — company metadata. `stateOfIncorporation`
  lives here and *only* here: companyfacts carries numeric facts, so a string
  fact like the domicile is absent from it entirely (verified against a live
  response).

Speaks httpx directly with `parse_float=Decimal`, the `TiingoProvider`
precedent — XBRL values arrive as JSON numbers and this is the only place the
money invariant can be lost.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx

from worker.config import EDGAR_USER_AGENT

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_DATA_BASE = "https://data.sec.gov"

# SEC's published ceiling. Stay under it rather than at it.
_DEFAULT_RATE_PER_SECOND = 8.0


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        rate_per_second: float = _DEFAULT_RATE_PER_SECOND,
    ) -> None:
        ua = user_agent if user_agent is not None else EDGAR_USER_AGENT
        if not ua:
            raise RuntimeError(
                "EDGAR_USER_AGENT is not set (see repo-root .env). The SEC requires a "
                "User-Agent naming a real contact, e.g. 'Session Brief (you@example.com)', "
                "and returns 403 without one."
            )
        self._ua = ua
        self._client = httpx.Client(transport=transport, timeout=60.0)
        self._min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._last_request = 0.0
        self._tickers: dict[str, str] | None = None

    # --- the seam ----------------------------------------------------------

    def cik_for(self, symbol: str) -> str | None:
        """Zero-padded 10-digit CIK, or None if EDGAR doesn't know the ticker.

        None is deliberate: a guessed CIK silently ingests a different
        company's filings, which is far worse than a missing row.
        """
        if self._tickers is None:
            raw = self._get(_TICKERS_URL)
            self._tickers = {
                str(entry["ticker"]).upper(): str(entry["cik_str"]).zfill(10)
                for entry in raw.values()
                if isinstance(entry, dict) and entry.get("ticker")
            }
        return self._tickers.get(symbol.upper())

    def company_facts(self, cik: str) -> dict[str, Any]:
        """Every numeric XBRL fact the registrant has filed."""
        return self._get(f"{_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json")

    def domicile(self, cik: str) -> str | None:
        """State or country of incorporation, from `submissions`. None when the
        registrant doesn't report it — never inferred."""
        payload = self._get(f"{_DATA_BASE}/submissions/CIK{cik}.json")
        state = payload.get("stateOfIncorporation")
        return str(state) if state else None

    # --- internals ---------------------------------------------------------

    def _get(self, url: str) -> dict[str, Any]:
        self._throttle()
        response = self._client.get(url, headers={"User-Agent": self._ua})

        if response.status_code == 403:
            raise RuntimeError(
                f"EDGAR returned 403 for {url}. This is almost always the User-Agent: "
                "the SEC requires one naming a real contact. Not retried — retrying "
                "into a block is how an IP gets banned."
            )
        response.raise_for_status()

        data = json.loads(response.text, parse_float=Decimal)
        if not isinstance(data, dict):
            raise ValueError(f"EDGAR returned non-object for {url}: {type(data).__name__}")
        return data

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._last_request + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
