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


def test_a_malformed_non_list_news_body_degrades_to_no_news() -> None:
    """`FdnClient.fetch` raises `ValueError` on a non-list body (a vendor error
    envelope, say) — the same failure shape as a 500, and must degrade the
    same way (M16 review, finding 1)."""
    client = FdnClient(
        "k", transport=httpx.MockTransport(lambda _r: httpx.Response(200, text='{"error":"nope"}'))
    )
    assert fetch_held_news(client, session_date=_SESSION, held={"ZHELD"}) == {}


def test_a_news_page_of_non_dict_elements_degrades_to_no_news() -> None:
    """A record that isn't a dict (`record.get(...)`) must degrade like any
    other bad response, not raise `AttributeError` out of the job (M16 review,
    finding 1)."""
    client = FdnClient(
        "k", transport=httpx.MockTransport(lambda _r: httpx.Response(200, text='["not-a-dict"]'))
    )
    assert fetch_held_news(client, session_date=_SESSION, held={"ZHELD"}) == {}
