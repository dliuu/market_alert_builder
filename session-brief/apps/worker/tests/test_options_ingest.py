"""M21 options ingest, pure: a MockTransport-backed CboeClient and a MagicMock
connection. The DB behaviour lives in test_options_ingest_db.py."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from worker.options import parse_occ
from worker.options_ingest import PullResult, pull_options
from worker.providers.cboe import STORED_MAX_DTE, CboeClient
from worker.providers.synthetic import SyntheticOptionsProvider

SESSION = date(2026, 10, 20)
_SYNTH = SyntheticOptionsProvider({"GOOD": Decimal("100.00")}, SESSION)


def _far_contract() -> dict[str, Any]:
    """One option outside the D38 band (too far to expiry), to prove the band
    filter runs before anything is stored."""
    return {**_SYNTH.chain("GOOD")["data"]["options"][0], "option": "GOOD281215C00100000"}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/delayed_quotes/options/GOOD.json"):
        payload = _SYNTH.chain("GOOD")
        payload["data"]["options"].append(_far_contract())
        return httpx.Response(200, text=json.dumps(payload, default=str))
    if path.endswith("/delayed_quotes/options/BAD.json"):
        return httpx.Response(500)
    if path.endswith("/us_indices/daily_prices/VIX_History.csv"):
        return httpx.Response(200, text=_SYNTH.index_history("VIX"))
    if path.endswith("/delayed_quotes/options/_VIX.json"):
        return httpx.Response(200, text=json.dumps(_SYNTH.index_quote("VIX"), default=str))
    return httpx.Response(404)


def _conn() -> MagicMock:
    conn = MagicMock()
    conn.execute.return_value.scalar.return_value = None  # no index watermark yet
    conn.execute.return_value.rowcount = 1
    return conn


def _raw_bodies(conn: MagicMock) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for call in conn.execute.call_args_list:
        params = call.args[1] if len(call.args) > 1 else None
        if isinstance(params, dict) and "body" in params:
            out[(params["endpoint"], params["symbol"])] = json.loads(params["body"])
    return out


def _pull(conn: MagicMock, indices: tuple[str, ...] = ("VIX",)) -> tuple[PullResult, MagicMock]:
    client = CboeClient(transport=httpx.MockTransport(_handler))
    with patch("worker.options_ingest.store_chain", return_value=20) as store:
        result = pull_options(conn, client, ["GOOD", "BAD"], SESSION, indices=indices)
    client.close()
    return result, store


def test_bad_symbol_is_recorded_and_good_symbol_is_stored() -> None:
    result, store = _pull(_conn())
    assert result.symbols_ok == ("GOOD",)
    assert set(result.symbols_failed) == {"BAD"}
    assert result.symbols_failed["BAD"].startswith("HTTP 500")
    assert store.call_count == 1
    assert store.call_args.args[1]["data"]["symbol"] == "GOOD"


def test_band_filter_runs_before_storage() -> None:
    conn = _conn()
    _, store = _pull(conn)
    stored = _raw_bodies(conn)[("delayed_quotes/options", "GOOD")]
    for payload in (stored, store.call_args.args[1]):
        expiries = [parse_occ(o["option"]).expiry for o in payload["data"]["options"]]
        assert expiries
        assert all((e - SESSION).days <= STORED_MAX_DTE for e in expiries)
        assert "GOOD281215C00100000" not in {o["option"] for o in payload["data"]["options"]}


def test_store_chain_gets_the_payload_timestamp_as_captured_at() -> None:
    _, store = _pull(_conn())
    captured_at = store.call_args.args[3]
    assert captured_at.isoformat() == "2026-10-20T21:00:00+00:00"


def test_index_history_and_quote_block_are_stored() -> None:
    conn = _conn()
    result, _ = _pull(conn)
    assert result.indices_ok == ("VIX",)
    assert result.indices_failed == {}
    bodies = _raw_bodies(conn)
    rows = bodies[("us_indices/daily_prices", "VIX")]["rows"]
    assert len(rows) == 300
    assert rows[0].count(",") == 4 and not rows[0].upper().startswith("DATE")
    quote = bodies[("delayed_quotes/index", "_VIX")]
    assert "options" not in quote["data"]
    assert quote["data"]["close"] == str(_SYNTH.index_quote("VIX")["data"]["close"])
    # raw rows: GOOD chain + VIX history + _VIX quote
    assert result.raw_rows == 3


def test_index_failure_is_recorded_not_raised() -> None:
    result, _ = _pull(_conn(), ("VIX", "VIX9D"))  # VIX9D is a 404 here
    assert result.indices_ok == ("VIX",)
    assert result.indices_failed == {"VIX9D": "HTTP 404 Not Found"}


def test_existing_raw_row_means_store_chain_is_not_called() -> None:
    conn = _conn()
    conn.execute.return_value.rowcount = 0  # every raw row already captured
    result, store = _pull(conn)
    assert store.call_count == 0
    assert result.symbols_ok == ("GOOD",)
    assert result.raw_rows == 0


def test_a_storage_bug_is_not_swallowed_as_a_feed_failure() -> None:
    """Only the feed half is caught: a TypeError from store_chain (a selection
    or storage bug) must propagate, not read as a quiet feed failure."""
    client = CboeClient(transport=httpx.MockTransport(_handler))
    with (
        patch("worker.options_ingest.store_chain", side_effect=TypeError("bug")),
        pytest.raises(TypeError, match="bug"),
    ):
        pull_options(_conn(), client, ["GOOD"], SESSION, indices=())
    client.close()
