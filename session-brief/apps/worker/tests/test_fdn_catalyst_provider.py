"""M17's live provider seam: FdnClient -> CatalystProvider, rows verbatim."""

from __future__ import annotations

import httpx

from worker.providers.fdn import FdnCatalystProvider, FdnClient


def test_insider_transactions_hit_the_endpoint_and_return_vendor_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text='[{"trading_symbol": "ASTS", "transaction_code": "P"}]')

    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(handler)))
    rows = provider.insider_transactions("ASTS")

    assert rows == [{"trading_symbol": "ASTS", "transaction_code": "P"}]  # verbatim
    assert seen[0].url.path.endswith("/insider-transactions")
    assert seen[0].url.params["identifier"] == "ASTS"
    assert seen[0].url.params["offset"] == "0"


def test_proposed_sales_hit_their_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="[]")

    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(handler)))
    assert provider.proposed_sales("ASTS", offset=100) == []
    assert seen[0].url.path.endswith("/proposed-sales")
    assert seen[0].url.params["offset"] == "100"


def test_public_float_is_none() -> None:
    """Q4 stands: the vendor's float is unproven, so `large_144` keeps its
    conservative `fundamentals.shares_out` denominator from `book_floats`."""
    provider = FdnCatalystProvider(FdnClient("k", transport=httpx.MockTransport(
        lambda _r: httpx.Response(500)
    )))
    assert provider.public_float("ASTS") is None  # no network call
