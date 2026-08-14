from __future__ import annotations

import httpx
import pytest

from worker.cli import _fdn_probe
from worker.providers.fdn import FdnClient


def test_probe_reports_every_route_and_survives_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("futures-prices"):
            return httpx.Response(500)
        return httpx.Response(
            200,
            text=(
                '[{"trading_symbol": "X", "price": 1.0, "change": 0.1, '
                '"time": "2026-08-14 12:00:00", "date": "2026-08-14", '
                '"close": 1.0, "volume": 100}]'
            ),
        )

    client = FdnClient("k", transport=httpx.MockTransport(handler))
    _fdn_probe(client, symbols=["ASTS"])  # must not raise
    out = capsys.readouterr().out
    assert "ES=F" in out and "✗" in out and "✓" in out


def test_probe_never_prints_the_api_key_on_a_failing_endpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FdnClient authenticates via a `key` query parameter, and this probe's
    whole purpose is to be read/pasted/screenshotted by a human — the worst
    place for `?key=<secret>` to leak. Every endpoint 500s here, so every
    failure line must go through `_safe_error` rather than `str(exc)` (which
    would embed the request URL, key included)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = FdnClient("SUPERSECRET", transport=httpx.MockTransport(handler))
    _fdn_probe(client, symbols=["ASTS"])  # must not raise
    out = capsys.readouterr().out
    assert "SUPERSECRET" not in out
    assert "500" in out
