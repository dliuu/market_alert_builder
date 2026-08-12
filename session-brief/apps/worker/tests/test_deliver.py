"""M9 delivery: Resend retry/fallback logic (no network) and, against a real DB
(skipped without DATABASE_URL), the idempotency guard — a second send of an
already-sent brief is skipped, never mailed twice."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker import config, deliver

_FIXTURE = Path(__file__).parent / "fixtures" / "close_brief.json"
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fc"
_D = date(2099, 3, 4)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("POST", "x"), response=None  # type: ignore[arg-type]
            )


def test_send_via_resend_returns_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(200, {"id": "msg_123"})
    )
    msg_id = deliver.send_via_resend(
        sender="s <a@b.co>", recipient="x@y.co", subject="hi", html="<p>hi</p>", text_part="hi"
    )
    assert msg_id == "msg_123"


def test_send_via_resend_retries_5xx_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _post(*a: Any, **k: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(503, {})

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # no real backoff in tests

    with pytest.raises(httpx.HTTPStatusError):
        deliver.send_via_resend(
            sender="s", recipient="x@y.co", subject="hi", html=None, text_part="hi"
        )
    assert calls["n"] == 3  # tried three times before giving up


def test_send_via_resend_4xx_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _post(*a: Any, **k: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(422, {})

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", _post)

    with pytest.raises(httpx.HTTPStatusError):
        deliver.send_via_resend(
            sender="s", recipient="x@y.co", subject="hi", html="<p>x</p>", text_part="x"
        )
    assert calls["n"] == 1  # a bad payload won't fix itself — no retry


def test_fallback_text_links_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "WEB_RENDER_URL", "https://app.example")
    obj = BriefObject.model_validate(json.loads(_FIXTURE.read_text()))
    txt = deliver._fallback_text(obj, date(2026, 8, 11), "close")
    assert obj.subject in txt
    assert "https://app.example/briefs/2026-08-11-close" in txt


# --- DB-backed idempotency (skipped without DATABASE_URL) ---


def _seed_brief(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-deliver@example.invalid')"),
        {"u": _TEST_USER_ID},
    )
    body = json.loads(_FIXTURE.read_text())
    conn.execute(
        text(
            "INSERT INTO briefs (user_id, session_date, kind, schema_version, body) "
            "VALUES (:u, :d, 'close', :v, CAST(:b AS jsonb))"
        ),
        {"u": _TEST_USER_ID, "d": _D, "v": body["schema_version"], "b": json.dumps(body)},
    )


def test_deliver_is_idempotent(db_conn: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_brief(db_conn)
    sends = {"n": 0}

    def _fake_send(**kwargs: Any) -> str:
        sends["n"] += 1
        return "msg_abc"

    monkeypatch.setattr(deliver, "render_brief", lambda _id: ("<p>hi</p>", "hi"))
    monkeypatch.setattr(deliver, "send_via_resend", _fake_send)

    first = deliver.deliver_brief(
        db_conn,
        user_id=_TEST_USER_ID,
        session_date=_D,
        kind="close",
        recipient="x@y.co",
        sender="s <a@b.co>",
    )
    assert first.status == "sent" and first.provider_msg_id == "msg_abc"

    second = deliver.deliver_brief(
        db_conn,
        user_id=_TEST_USER_ID,
        session_date=_D,
        kind="close",
        recipient="x@y.co",
        sender="s <a@b.co>",
    )
    assert second.status == "skipped"
    assert sends["n"] == 1  # the second call never reached Resend

    status = db_conn.execute(
        text("SELECT status FROM deliveries WHERE recipient = 'x@y.co'")
    ).scalar_one()
    assert status == "sent"


def test_deliver_falls_back_when_render_down(
    db_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_brief(db_conn)
    captured: dict[str, Any] = {}

    def _down(_id: str) -> tuple[str, str]:
        raise httpx.ConnectError("render endpoint down")

    def _fake_send(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "msg_fallback"

    monkeypatch.setattr(deliver, "render_brief", _down)
    monkeypatch.setattr(deliver, "send_via_resend", _fake_send)

    result = deliver.deliver_brief(
        db_conn,
        user_id=_TEST_USER_ID,
        session_date=_D,
        kind="close",
        recipient="x@y.co",
        sender="s <a@b.co>",
    )
    assert result.status == "sent"
    assert captured["html"] is None  # text-only send
    assert captured["text_part"]  # a non-empty plaintext part was still produced
