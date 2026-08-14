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
