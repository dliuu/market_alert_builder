"""Held-name news (M16): fdn latest-news → the §3 has_news gate + narration + close brief.

Ten records per call with offset pagination; three pages is plenty for a
morning gate. Headlines flow to exactly two places — `clears_threshold`'s
`has_news` and the open narration prompt (docs/04 rule 2: attributing a move
to a cause is the one thing the model does better than the pipeline). They are
never rendered directly and never stored outside raw_payloads, which keeps the
redistribution surface at zero. Failures degrade to no news, never a crash.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from worker.providers.fdn import FEED_ERRORS, FdnClient

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
            if not records:
                break
            for record in records:
                headline = str(record.get("article_headline") or "").strip()
                if not headline:
                    continue
                for symbol in record.get("trading_symbols") or []:
                    if (
                        str(symbol) in held
                        and len(out.setdefault(str(symbol), [])) < _PER_SYMBOL_CAP
                    ):
                        out[str(symbol)].append(headline)
        except FEED_ERRORS:
            break
    return out


_WEEK_DAYS = 7
_MAX_PAGES_PER_DAY = 30   # live days ran ~20 pages of 10 (probe 2026-09-04)
_WEEK_PER_SYMBOL_CAP = 5
_THROTTLE_S = 0.2         # paging flat-out drew 429s on the live key


def fetch_week_news(
    client: FdnClient, *, session_date: date, held: set[str]
) -> dict[str, list[str]]:
    """The close brief's news context (docs/04 rule 2, same as the open's):
    every held-name headline from the past ``_WEEK_DAYS`` calendar days,
    newest day first. ``latest-news`` has no per-symbol filter at any tier
    (Q10, probed 2026-09-04), so each day's market-wide feed is paged to
    exhaustion and filtered here. A day that fails mid-page keeps the pages it
    got and the loop moves on — one bad day loses a day, never the week.
    Headlines flow only to close narration; the redistribution surface stays
    zero (see the module header)."""
    out: dict[str, list[str]] = {}
    for back in range(_WEEK_DAYS):
        day = session_date - timedelta(days=back)
        try:
            for page in range(_MAX_PAGES_PER_DAY):
                records = client.fetch(
                    "latest-news", date=day.isoformat(), offset=str(page * 10)
                )
                if not records:
                    break
                for record in records:
                    headline = str(record.get("article_headline") or "").strip()
                    if not headline:
                        continue
                    for symbol in record.get("trading_symbols") or []:
                        if (
                            str(symbol) in held
                            and len(out.setdefault(str(symbol), [])) < _WEEK_PER_SYMBOL_CAP
                        ):
                            out[str(symbol)].append(headline)
                if len(records) < 10:
                    break
                time.sleep(_THROTTLE_S)
        except FEED_ERRORS:
            continue
    return out
