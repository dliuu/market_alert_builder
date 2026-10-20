from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from worker.providers.cboe import (
    CboeClient,
    filter_payload_for_storage,
    parse_chain,
    parse_index_csv,
)

SESSION = date(2026, 10, 20)


# --- parse_chain -------------------------------------------------------------


def _payload() -> dict[str, Any]:
    return {
        "timestamp": "2026-10-20 23:08:56",
        "data": {
            "symbol": "AAPL",
            "current_price": Decimal("338.31"),
            "close": Decimal("339.5"),
            "prev_day_close": Decimal("342.94"),
            "iv30": Decimal("26.831"),
            "options": [
                {
                    "option": "AAPL261121C00340000",
                    "bid": Decimal("8.0"),
                    "bid_size": Decimal("10.0"),
                    "ask_size": Decimal("12.0"),
                    "gamma": Decimal("0.0151"),
                    "rho": Decimal("0.0312"),
                    "ask": Decimal("8.65"),
                    "iv": Decimal("0.2999"),
                    "delta": Decimal("0.5151"),
                    "theta": Decimal("-0.2759"),
                    "vega": Decimal("0.275"),
                    "open_interest": Decimal("1906.0"),
                    "volume": Decimal("75.0"),
                    "last_trade_price": Decimal("8.23"),
                    "prev_day_close": Decimal("9.175"),
                    "percent_change": Decimal("-31.3352"),
                },
                {
                    "option": "AAPL261121C00250000",
                    "bid": Decimal("87.65"),
                    "ask": Decimal("91.4"),
                    "iv": Decimal("0.0"),
                    "open_interest": Decimal("1.0"),
                    "volume": Decimal("34.0"),
                },
            ],
        },
    }


def test_parse_chain_normalises_units_and_treats_zero_iv_as_none() -> None:
    underlying, contracts = parse_chain(_payload())
    assert underlying.symbol == "AAPL"
    assert underlying.close == Decimal("339.5")
    assert underlying.iv30 == Decimal("26.831")
    assert underlying.timestamp == datetime(2026, 10, 20, 23, 8, 56, tzinfo=UTC)
    atm, deep = contracts
    assert atm.iv == Decimal("29.99")  # fraction → vol points
    assert atm.open_interest == 1906 and atm.volume == 75  # floats → ints
    assert atm.mid == Decimal("8.325") and atm.two_sided
    assert deep.iv is None  # Cboe's 0.0 means "none"
    assert deep.delta is None
    assert (atm.bid_size, atm.ask_size) == (10, 12)
    assert atm.gamma == Decimal("0.0151") and atm.rho == Decimal("0.0312")
    assert deep.bid_size == 0 and deep.gamma is None


def test_parse_chain_raises_on_a_missing_quote_field() -> None:
    payload = _payload()
    del payload["data"]["options"][0]["ask"]
    with pytest.raises(KeyError):
        parse_chain(payload)


def test_filter_payload_for_storage_keeps_the_underlying_and_the_near_middle() -> None:
    payload = _payload()
    payload["data"]["options"] += [
        {"option": "AAPL290119C00340000", "bid": 1, "ask": 2},  # 2029: too far out
        {"option": "AAPL261121C00500000", "bid": 1, "ask": 2},  # +47%: too far from spot
    ]
    kept = filter_payload_for_storage(payload, SESSION, max_dte=60)
    assert kept["data"]["iv30"] == Decimal("26.831")
    # The 250 strike is -26% from a 339.5 close: outside the ±25% band too.
    assert [o["option"] for o in kept["data"]["options"]] == ["AAPL261121C00340000"]
    assert kept["data"]["options"][0] is payload["data"]["options"][0]  # as published


# --- index csv ---------------------------------------------------------------


def test_parse_index_csv_reads_cboe_history_rows() -> None:
    text = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/1990,17.24,17.24,17.24,17.24\n"
        "10/20/2026,18.1,19.0,17.9,18.44\n"
    )
    bars = parse_index_csv(text)
    assert bars[0].session_date == date(1990, 1, 2)
    assert bars[-1].session_date == date(2026, 10, 20) and bars[-1].c == Decimal("18.44")


def test_parse_index_csv_rejects_other_files() -> None:
    with pytest.raises(ValueError):
        parse_index_csv("<html>not a csv</html>")


# --- client ------------------------------------------------------------------


def test_client_follows_the_host_redirect_sends_a_user_agent_and_captures() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "cdn.cboe.com":
            return httpx.Response(
                307, headers={"location": str(request.url.copy_with(host="cdn-api.cboe.com"))}
            )
        if request.url.path.endswith("/AAPL.json"):
            return httpx.Response(
                200, text='{"timestamp": "2026-10-20 23:08:56", "data": {"options": []}}'
            )
        if request.url.path.endswith("/_VIX.json"):
            return httpx.Response(
                200, text='{"timestamp": "2026-10-20 23:08:56", "data": {"symbol": "VIX"}}'
            )
        if request.url.path.endswith("/VIX_History.csv"):
            return httpx.Response(200, text="DATE,OPEN,HIGH,LOW,CLOSE\n10/20/2026,1,2,0.5,1.5\n")
        return httpx.Response(404)

    client = CboeClient(
        base_url="https://cdn.cboe.com/api/global", transport=httpx.MockTransport(handler)
    )
    payload = client.chain("aapl")
    assert payload["data"]["options"] == []
    assert client.index_quote("VIX")["data"]["symbol"] == "VIX"
    assert client.index_history("^VIX").startswith("DATE,")
    assert seen[0].headers["user-agent"].startswith("session-brief/")
    assert seen[-1].url.host == "cdn-api.cboe.com"
    assert [(e, s) for e, s, _ in client.captured] == [
        ("delayed_quotes/options", "AAPL"),
        ("delayed_quotes/index", "_VIX"),
        ("us_indices/daily_prices", "VIX"),
    ]


def test_client_rejects_a_chain_without_an_options_array() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"timestamp": "x", "data": {"symbol": "AAPL"}}')

    client = CboeClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        client.chain("AAPL")
    assert client.captured == []
