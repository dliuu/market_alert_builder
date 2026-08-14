from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from worker.providers.fdn import FdnClient, FdnProvider


def test_latest_minute_uses_the_injected_fetcher() -> None:
    def fake(symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "ts": datetime(2020, 6, 30, 20, 0, tzinfo=UTC),
                "price": Decimal("51.25")}

    p = FdnProvider(latest_minute_fn=fake)
    got = p.latest_minute("NVDA")
    assert got["price"] == Decimal("51.25") and got["symbol"] == "NVDA"


def test_latest_minute_without_a_feed_refuses_rather_than_guessing() -> None:
    p = FdnProvider()  # no key, no injected fn
    with pytest.raises((RuntimeError, NotImplementedError)):
        p.latest_minute("NVDA")


def _client(handler: object) -> FdnClient:
    return FdnClient("k", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_fetch_parses_prices_as_decimal_and_sends_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "k"
        assert request.url.path == "/api/v1/latest-prices"
        assert request.url.params["identifier"] == "ASTS"
        return httpx.Response(200, text='[{"trading_symbol": "ASTS", "close": 74.31}]')

    got = _client(handler).fetch("latest-prices", identifier="ASTS")
    assert got == [{"trading_symbol": "ASTS", "close": Decimal("74.31")}]
    assert not isinstance(got[0]["close"], float)


def test_fetch_captures_the_verbatim_response_for_raw_payloads() -> None:
    body = '[{"trading_symbol": "ES", "close": 5620.0}]'
    client = _client(lambda _req: httpx.Response(200, text=body))
    client.fetch("futures-prices", identifier="ES")
    assert client.captured == [("futures-prices", "ES", body)]


def test_fetch_refuses_a_non_list_body() -> None:
    client = _client(lambda _req: httpx.Response(200, text='{"error": "nope"}'))
    with pytest.raises(ValueError):
        client.fetch("latest-prices", identifier="ASTS")


def test_client_without_a_key_refuses() -> None:
    with pytest.raises(RuntimeError):
        FdnClient("")
