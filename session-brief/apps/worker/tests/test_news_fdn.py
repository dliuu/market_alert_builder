"""Held-name news (M16): fdn latest-news → the §3 has_news gate + narration."""

from __future__ import annotations

from datetime import date

import httpx

from worker.news_fdn import fetch_held_news
from worker.providers.fdn import FdnClient

_SESSION = date(2026, 8, 14)


def test_held_news_filters_to_the_book_and_caps_at_three() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset", "0")
        if offset != "0":
            return httpx.Response(200, text="[]")
        return httpx.Response(200, text=(
            '[{"trading_symbols": ["ZHELD"], "article_headline": "h1"},'
            ' {"trading_symbols": ["ZHELD", "ZOTHER"], "article_headline": "h2"},'
            ' {"trading_symbols": ["ZOTHER"], "article_headline": "h3"},'
            ' {"trading_symbols": ["ZHELD"], "article_headline": "h4"},'
            ' {"trading_symbols": ["ZHELD"], "article_headline": "h5"}]'
        ))

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_held_news(client, session_date=_SESSION, held={"ZHELD"})
    assert got == {"ZHELD": ["h1", "h2", "h4"]}


def test_a_news_500_degrades_to_no_news() -> None:
    client = FdnClient("k", transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    assert fetch_held_news(client, session_date=_SESSION, held={"ZHELD"}) == {}
