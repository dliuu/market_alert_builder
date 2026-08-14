"""M18: the EDGAR client. No test makes a live call — every request goes
through an httpx MockTransport.

SEC requires a contact `User-Agent` and rate-limits to 10 req/s across its
hosts. Both are conditions of use, not preferences, so both are tested.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from worker.providers.edgar import EdgarClient

_UA = "Session Brief (dev@example.invalid)"

_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
}


def transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, text=json.dumps(payload))


def test_a_ticker_resolves_to_its_zero_padded_cik() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("company_tickers.json")
        return ok(_TICKERS)

    client = EdgarClient(user_agent=_UA, transport=transport(handler))

    assert client.cik_for("AAPL") == "0000320193"


def test_ticker_lookup_is_case_insensitive() -> None:
    client = EdgarClient(user_agent=_UA, transport=transport(lambda r: ok(_TICKERS)))

    assert client.cik_for("aapl") == "0000320193"


def test_an_unknown_ticker_returns_none_rather_than_guessing() -> None:
    """Guessing a CIK means silently ingesting a different company's filings."""
    client = EdgarClient(user_agent=_UA, transport=transport(lambda r: ok(_TICKERS)))

    assert client.cik_for("NOTREAL") is None


def test_the_ticker_map_is_fetched_once_and_cached() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok(_TICKERS)

    client = EdgarClient(user_agent=_UA, transport=transport(handler))
    client.cik_for("AAPL")
    client.cik_for("MSFT")

    assert calls == 1


def test_every_request_carries_the_contact_user_agent() -> None:
    """SEC returns 403 without it; this is a documented condition of use."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return ok(_TICKERS)

    EdgarClient(user_agent=_UA, transport=transport(handler)).cik_for("AAPL")

    assert seen == [_UA]


def test_construction_without_a_user_agent_refuses() -> None:
    """Better a loud failure at construction than a 403 mid-run, or worse, a
    polite-looking crawler the SEC blocks."""
    with pytest.raises(RuntimeError, match="EDGAR_USER_AGENT"):
        EdgarClient(user_agent="", transport=transport(lambda r: ok({})))


def test_company_facts_parses_numbers_as_decimal() -> None:
    """XBRL values arrive as JSON numbers; this is the only place the money
    invariant can be lost."""
    payload = {"cik": 320193, "facts": {"us-gaap": {"X": {"units": {"USD": [
        {"val": 0.07, "end": "2026-06-27", "filed": "2026-07-31", "form": "10-Q",
         "accn": "a", "fp": "Q3"},
    ]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "CIK0000320193.json" in request.url.path
        return ok(payload)

    client = EdgarClient(user_agent=_UA, transport=transport(handler))
    got = client.company_facts("0000320193")

    assert isinstance(got["facts"]["us-gaap"]["X"]["units"]["USD"][0]["val"], Decimal)


def test_domicile_comes_from_submissions_not_companyfacts() -> None:
    """companyfacts carries numeric facts only, so the state of incorporation
    is simply not in it — verified against a live response."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "submissions" in request.url.path
        return ok({"cik": "0000320193", "stateOfIncorporation": "CA"})

    client = EdgarClient(user_agent=_UA, transport=transport(handler))

    assert client.domicile("0000320193") == "CA"


def test_a_missing_domicile_is_none_not_an_error() -> None:
    client = EdgarClient(user_agent=_UA, transport=transport(lambda r: ok({"cik": "x"})))

    assert client.domicile("0000320193") is None


def test_a_403_is_reported_as_a_configuration_problem() -> None:
    """Retrying into an SEC ban is worse than failing. The message has to point
    at the User-Agent, which is the only thing that causes this."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = EdgarClient(user_agent=_UA, transport=transport(handler))

    with pytest.raises(RuntimeError, match="User-Agent"):
        client.company_facts("0000320193")


def test_the_rate_limiter_holds_under_the_sec_ceiling() -> None:
    """10 req/s across SEC hosts. The limiter is what keeps a book-wide refresh
    from tripping it."""
    import time

    client = EdgarClient(user_agent=_UA, transport=transport(lambda r: ok({"cik": 1})),
                         rate_per_second=20)
    start = time.monotonic()
    for _ in range(6):
        client.company_facts("0000000001")
    elapsed = time.monotonic() - start

    # 6 requests at 20/s cannot finish faster than 5 gaps of 50ms.
    assert elapsed >= 0.20
