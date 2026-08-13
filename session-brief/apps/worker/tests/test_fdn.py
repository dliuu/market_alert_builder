from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from worker.providers.fdn import FdnProvider


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
