"""The pure normalize core: field mapping, precision, and determinism. No DB."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from worker.normalize import Payload, bars_from_payloads


def _day(session: str, close: str, **over: Any) -> dict[str, Any]:
    record = {
        "date": f"{session}T00:00:00.000Z",
        "open": "1.0",
        "high": "2.0",
        "low": "0.5",
        "close": close,
        "volume": 1000,
        "adjClose": close,
    }
    record.update(over)
    return record


def _payload(symbol: str, fetched_at: datetime, records: list[dict[str, Any]]) -> Payload:
    return Payload(symbol=symbol, fetched_at=fetched_at, body=json.dumps(records, default=str))


_FETCH = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)


def test_maps_fields_and_derives_session_date() -> None:
    record = _day(
        "2026-08-07", "157.92", open="154.89", high="158.85", low="154.23", volume=37039737
    )
    (bar,) = bars_from_payloads([_payload("AAPL", _FETCH, [record])])

    assert bar.symbol == "AAPL"
    assert bar.session_date.isoformat() == "2026-08-07"
    assert bar.o == Decimal("154.89")
    assert bar.h == Decimal("158.85")
    assert bar.l == Decimal("154.23")
    assert bar.c == Decimal("157.92")
    assert bar.v == 37039737


def test_preserves_sub_cent_precision_without_float() -> None:
    record = _day("2026-08-07", "10.0", adjClose="123.456789")
    (bar,) = bars_from_payloads([_payload("AAPL", _FETCH, [record])])
    assert bar.adj_c == Decimal("123.456789")


def test_latest_fetch_wins_on_overlap_regardless_of_order() -> None:
    early = _payload("AAPL", datetime(2026, 8, 7, 10, 0, tzinfo=UTC), [_day("2026-08-07", "100")])
    late = _payload("AAPL", datetime(2026, 8, 7, 20, 0, tzinfo=UTC), [_day("2026-08-07", "200")])

    (forward,) = bars_from_payloads([early, late])
    (reverse,) = bars_from_payloads([late, early])

    assert forward.c == Decimal("200")
    assert reverse.c == Decimal("200")


def test_output_sorted_by_symbol_then_date() -> None:
    payloads = [
        _payload("MSFT", _FETCH, [_day("2026-08-06", "300")]),
        _payload("AAPL", _FETCH, [_day("2026-08-07", "150"), _day("2026-08-06", "149")]),
    ]
    keys = [(bar.symbol, bar.session_date.isoformat()) for bar in bars_from_payloads(payloads)]
    assert keys == [
        ("AAPL", "2026-08-06"),
        ("AAPL", "2026-08-07"),
        ("MSFT", "2026-08-06"),
    ]


def test_empty_input() -> None:
    assert bars_from_payloads([]) == []
