from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker import cli, config, options_ingest
from worker.cli import _options_probe, _options_probe_cmd
from worker.providers.cboe import CboeClient

SESSION = date(2026, 10, 20)


def _chain_text(symbol: str) -> str:
    spot = 100
    options = []
    for days in (8, 15, 22, 29, 43, 90):
        expiry = SESSION + timedelta(days=days)
        for pct in (-10, -5, 0, 5, 10):
            strike = spot * (100 + pct) // 100
            for right in ("C", "P"):
                options.append(
                    {
                        "option": f"{symbol}{expiry:%y%m%d}{right}{strike * 1000:08d}",
                        "bid": 1.0,
                        "ask": 1.2,
                        "iv": 0.30,
                        "delta": 0.5,
                        "theta": -0.1,
                        "vega": 0.2,
                        "open_interest": 10.0,
                        "volume": 3.0,
                    }
                )
    return json.dumps(
        {
            "timestamp": "2026-10-20 23:08:56",
            "data": {
                "symbol": symbol,
                "current_price": 99.5,
                "close": 100.0,
                "prev_day_close": 101.0,
                "iv30": 30.1,
                "options": options,
            },
        }
    )


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/BAD.json"):
        return httpx.Response(500)
    if path.endswith("/_VIX.json"):
        return httpx.Response(
            200,
            text=(
                '{"timestamp": "2026-10-20 23:08:56", "data": {"symbol": "VIX", '
                '"close": 18.44, "current_price": 18.5, "options": []}}'
            ),
        )
    if path.endswith(".json"):
        return httpx.Response(200, text=_chain_text(path.rsplit("/", 1)[1][:-5]))
    if path.endswith("/VIX_History.csv"):
        return httpx.Response(200, text="DATE,OPEN,HIGH,LOW,CLOSE\n10/20/2026,18,19,17,18.44\n")
    return httpx.Response(404)


def test_probe_prints_the_strip_and_iv30_and_survives_a_bad_symbol(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = CboeClient(transport=httpx.MockTransport(_handler))
    _options_probe(
        client,
        symbols=["GOOD", "BAD"],
        indices=["VIX", "VIX9D"],
        session_date=SESSION,
        save_dir=tmp_path,
    )  # must not raise
    out = capsys.readouterr().out
    assert "✓ GOOD: 60 contracts, 6 expiries" in out
    assert "cboe iv30=30.10" in out
    assert "IV30 ours=30.00" in out and "drift vs cboe=-0.10" in out
    assert out.count("itm10") == 4 and out.count("otm10") == 4  # twelve cells
    assert "✗ BAD: HTTP 500" in out
    assert "✓ VIX: 1 rows" in out
    assert "VIX delayed quote: close=18.44 current=18.5" in out
    assert "✗ VIX9D: HTTP 404" in out
    assert (tmp_path / "GOOD.json").read_text() == _chain_text("GOOD")
    assert not (tmp_path / "BAD.json").exists()


def test_probe_cmd_reads_a_saved_file_offline(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    saved = tmp_path / "GOOD.json"
    saved.write_text(_chain_text("GOOD"))
    _options_probe_cmd(
        symbols_arg=None,
        file_arg=str(saved),
        date_arg="2026-10-20",
        indices_arg="VIX",
        save_dir=None,
    )
    out = capsys.readouterr().out
    assert "✓ GOOD: 60 contracts" in out
    assert "stored payload: 50 contracts ≤60 dte" in out  # the 90-day expiry is dropped


class _Engine:
    """Stands in for ``get_engine()``: ``begin()`` hands out the test's
    rolled-back connection."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def begin(self) -> _Begin:
        return _Begin(self._conn)


class _Begin:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def __enter__(self) -> Connection:
        return self._conn

    def __exit__(self, *exc: object) -> None:
        return None


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("CboeClient must not be constructed when OPTIONS_LIVE is off")


def test_pull_uses_the_synthetic_provider_when_not_live(
    db_conn: Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES ('ZZOPT', :d, 100, 100, 100, 100, 1000, 100)"
        ),
        {"d": SESSION},
    )
    monkeypatch.setattr(config, "OPTIONS_LIVE", False)
    monkeypatch.setattr(options_ingest, "CboeClient", _boom)
    monkeypatch.setattr(cli, "get_engine", lambda: _Engine(db_conn))
    cli._options_pull(date_arg=SESSION.isoformat(), symbols_arg="zzopt")
    out = capsys.readouterr().out
    assert f"options pull {SESSION}" in out and "ZZOPT" in out
    assert (
        db_conn.execute(
            text("SELECT count(*) FROM raw_payloads WHERE source = 'cboe' AND symbol = 'ZZOPT'")
        ).scalar_one()
        == 1
    )

    cli._options_rebuild(date_arg=SESSION.isoformat())
    assert f"options rebuild {SESSION}:" in capsys.readouterr().out
