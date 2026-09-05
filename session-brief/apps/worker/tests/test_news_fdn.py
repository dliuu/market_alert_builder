"""Held-name news (M16): fdn latest-news → the §3 has_news gate + narration."""

from __future__ import annotations

from datetime import date

import httpx

from worker import news_fdn
from worker.news_fdn import fetch_held_news, fetch_week_news
from worker.providers.fdn import FdnClient

news_fdn._THROTTLE_S = 0  # tests must not sleep between mock pages

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


def test_week_news_walks_seven_days_newest_first_and_caps_at_five() -> None:
    dates_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        d = request.url.params["date"]
        if request.url.params.get("offset", "0") != "0":
            return httpx.Response(200, text="[]")
        dates_seen.append(d)
        return httpx.Response(
            200, text=f'[{{"trading_symbols": ["ZHELD"], "article_headline": "h {d}"}}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert dates_seen == [
        "2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11",
        "2026-08-10", "2026-08-09", "2026-08-08",
    ]
    # Newest day first, capped at five — the freshest week survives the cap.
    assert got == {"ZHELD": [
        "h 2026-08-14", "h 2026-08-13", "h 2026-08-12", "h 2026-08-11", "h 2026-08-10",
    ]}


def test_week_news_pages_a_day_until_the_short_page() -> None:
    """A full page (10 records) means another page may follow; a short one ends
    the day. Live days ran ~20 pages deep (probe 2026-09-04)."""
    calls: list[tuple[str, str]] = []
    full = "[" + ",".join(
        '{"trading_symbols": [], "article_headline": "x"}' for _ in range(10)
    ) + "]"

    def handler(request: httpx.Request) -> httpx.Response:
        d, off = request.url.params["date"], request.url.params.get("offset", "0")
        calls.append((d, off))
        if d == _SESSION.isoformat() and off in ("0", "10"):
            return httpx.Response(200, text=full)
        return httpx.Response(
            200, text='[{"trading_symbols": ["ZHELD"], "article_headline": "deep"}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert (_SESSION.isoformat(), "20") in calls  # paged past two full pages
    assert "deep" in got["ZHELD"]


def test_week_news_a_bad_day_degrades_to_the_other_days() -> None:
    """One day 500ing (or 429ing) loses that day, not the week — the same
    contract as fetch_held_news, held per-day."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == _SESSION.isoformat():
            return httpx.Response(429)
        if request.url.params.get("offset", "0") != "0":
            return httpx.Response(200, text="[]")
        return httpx.Response(
            200, text='[{"trading_symbols": ["ZHELD"], "article_headline": "older"}]'
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    got = fetch_week_news(client, session_date=_SESSION, held={"ZHELD"})

    assert got == {"ZHELD": ["older"] * 5}  # six good days, capped at five
