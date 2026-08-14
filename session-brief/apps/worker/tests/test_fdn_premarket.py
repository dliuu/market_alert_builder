from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from worker.providers.fdn import FdnClient, FdnPremarketProvider

_SESSION = date(2026, 8, 14)  # EDT: pre-market window is 08:00–12:12 UTC


def _provider(handler: object, closes: dict[str, Decimal]) -> FdnPremarketProvider:
    client = FdnClient("k", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return FdnPremarketProvider(client, closes, _SESSION)


def _minute(time: str, close: str, volume: str) -> str:
    return (f'{{"trading_symbol": "ASTS", "time": "{time}", "open": {close}, '
            f'"high": {close}, "low": {close}, "close": {close}, "volume": {volume}}}')


def test_latest_prices_filters_to_the_premarket_window_and_sums_volume() -> None:
    body = "[" + ",".join([
        _minute("2026-08-13 19:59:00", "70.00", "9000"),   # yesterday: out
        _minute("2026-08-14 07:59:00", "71.00", "500"),    # 03:59 ET: out
        _minute("2026-08-14 08:01:00", "74.10", "1200"),   # 04:01 ET: in
        _minute("2026-08-14 12:10:00", "74.55", "800"),    # 08:10 ET: in, last
        _minute("2026-08-14 12:30:00", "75.00", "600"),    # after capture: out
    ]) + "]"
    p = _provider(lambda _r: httpx.Response(200, text=body), {"ASTS": Decimal("74.31")})
    got = p.get_latest_prices(["ASTS"])
    assert got == [{
        "symbol": "ASTS",
        "extended_last": Decimal("74.55"),
        "extended_v": 2000,
        "prev_close": Decimal("74.31"),
    }]


def test_latest_prices_omits_a_name_with_no_prior_close() -> None:
    p = _provider(lambda _r: httpx.Response(200, text="[]"), {})
    assert p.get_latest_prices(["ASTS"]) == []


def test_latest_prices_omits_a_name_with_no_window_prints() -> None:
    body = "[" + _minute("2026-08-13 19:59:00", "70.00", "9000") + "]"
    p = _provider(lambda _r: httpx.Response(200, text=body), {"ASTS": Decimal("74.31")})
    assert p.get_latest_prices(["ASTS"]) == []


def test_latest_prices_survives_a_vendor_500_by_omitting() -> None:
    p = _provider(lambda _r: httpx.Response(500), {"ASTS": Decimal("74.31")})
    assert p.get_latest_prices(["ASTS"]) == []


def test_latest_prices_survives_a_malformed_minute_by_omitting() -> None:
    """A minute record missing the shape `_parse_fdn_time` expects (`r["time"]`
    on something that isn't a dict) must omit the name, not raise out of the
    job (M16 review, finding 1) — the same degradation a 500 gets above."""
    body = "[" + '"not-a-dict"' + "]"
    p = _provider(lambda _r: httpx.Response(200, text=body), {"ASTS": Decimal("74.31")})
    assert p.get_latest_prices(["ASTS"]) == []


def test_index_quotes_derive_prev_close_from_price_minus_change() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/index-quotes"
        assert request.url.params["identifiers"] == "^TNX,^VIX"
        return httpx.Response(200, text=(
            '[{"trading_symbol": "^TNX", "price": 4.25, "change": 0.03},'
            ' {"trading_symbol": "^VIX", "price": 15.50, "change": -0.75}]'
        ))

    got = _provider(handler, {}).get_index_quotes(["^TNX", "^VIX"])
    assert got == [
        {"symbol": "^TNX", "last": Decimal("4.25"), "prev_close": Decimal("4.22")},
        {"symbol": "^VIX", "last": Decimal("15.50"), "prev_close": Decimal("16.25")},
    ]


def test_forex_route_maps_dxy_to_the_index_endpoint_and_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/index-quotes"
        assert request.url.params["identifiers"] == "^DXY"
        return httpx.Response(
            200, text='[{"trading_symbol": "^DXY", "price": 103.40, "change": 0.20}]'
        )

    got = _provider(handler, {}).get_forex_quotes(["DXY"])
    assert got == [
        {"symbol": "DXY", "last": Decimal("103.40"), "prev_close": Decimal("103.20")}
    ]


def test_futures_use_the_session_dated_bar_over_the_prior_settle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/futures-prices"
        assert request.url.params["identifier"] == "ES"
        return httpx.Response(200, text=(
            '[{"trading_symbol": "ES", "date": "2026-08-13", "close": 5600.00},'
            ' {"trading_symbol": "ES", "date": "2026-08-14", "close": 5620.00}]'
        ))

    got = _provider(handler, {}).get_futures_prices(["ES=F"])
    assert got == [
        {"symbol": "ES=F", "last": Decimal("5620.00"), "prev_close": Decimal("5600.00")}
    ]


def test_futures_with_no_session_dated_bar_are_omitted() -> None:
    body = '[{"trading_symbol": "ES", "date": "2026-08-13", "close": 5600.00}]'
    got = _provider(lambda _r: httpx.Response(200, text=body), {}).get_futures_prices(["ES=F"])
    assert got == []


def test_an_unmapped_tape_symbol_is_omitted_without_a_fetch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch for an unmapped symbol")

    assert _provider(handler, {}).get_index_quotes(["^UNMAPPED"]) == []


def test_a_tape_endpoint_500_yields_an_empty_feed() -> None:
    got = _provider(lambda _r: httpx.Response(500), {}).get_index_quotes(["^TNX"])
    assert got == []


def test_a_duplicated_futures_symbol_yields_one_row_and_one_fetch() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=(
            '[{"trading_symbol": "ES", "date": "2026-08-13", "close": 5600.00},'
            ' {"trading_symbol": "ES", "date": "2026-08-14", "close": 5620.00}]'
        ))

    got = _provider(handler, {}).get_futures_prices(["ES=F", "ES=F"])
    assert got == [
        {"symbol": "ES=F", "last": Decimal("5620.00"), "prev_close": Decimal("5600.00")}
    ]
    assert calls == 1
