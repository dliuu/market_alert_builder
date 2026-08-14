"""Held-name news (M16): fdn latest-news → the §3 has_news gate + narration.

Ten records per call with offset pagination; three pages is plenty for a
morning gate. Headlines flow to exactly two places — `clears_threshold`'s
`has_news` and the open narration prompt (docs/04 rule 2: attributing a move
to a cause is the one thing the model does better than the pipeline). They are
never rendered directly and never stored outside raw_payloads, which keeps the
redistribution surface at zero. Failures degrade to no news, never a crash.
"""

from __future__ import annotations

from datetime import date

import httpx

from worker.providers.fdn import FdnClient

_PAGES = 3
_PER_SYMBOL_CAP = 3


def fetch_held_news(
    client: FdnClient, *, session_date: date, held: set[str]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for page in range(_PAGES):
        try:
            records = client.fetch(
                "latest-news", date=session_date.isoformat(), offset=str(page * 10)
            )
        except httpx.HTTPError:
            break
        if not records:
            break
        for record in records:
            headline = str(record.get("article_headline") or "").strip()
            if not headline:
                continue
            for symbol in record.get("trading_symbols") or []:
                if str(symbol) in held and len(out.setdefault(str(symbol), [])) < _PER_SYMBOL_CAP:
                    out[str(symbol)].append(headline)
    return out
