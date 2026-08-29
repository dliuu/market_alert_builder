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


# --- Resend idempotency (the 2026-08-28 retry's safety net) ------------------
#
# `deliver_brief` writes the row that proves a send happened, but its *caller*
# commits. The window between Resend accepting the POST and that commit landing
# is owned by a pooled connection — and a connection death in that window is the
# exact failure the crashed-close retry now re-fires on. The DB evidence rolls
# back; the email is already out. A stable idempotency key is what stops the
# retry sending it twice, at the only place that knows the truth: the provider.


def test_send_via_resend_passes_the_idempotency_key_as_a_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _post(_url: str, **kwargs: Any) -> _FakeResponse:
        seen.update(kwargs["headers"])
        return _FakeResponse(200, {"id": "msg_123"})

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", _post)

    deliver.send_via_resend(
        sender="s <a@b.co>",
        recipient="x@y.co",
        subject="hi",
        html=None,
        text_part="hi",
        idempotency_key="brief-1:x@y.co",
    )
    assert seen["Idempotency-Key"] == "brief-1:x@y.co"


def test_send_via_resend_omits_the_header_when_no_key_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _post(_url: str, **kwargs: Any) -> _FakeResponse:
        seen.update(kwargs["headers"])
        return _FakeResponse(200, {"id": "msg_123"})

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", _post)

    deliver.send_via_resend(
        sender="s <a@b.co>", recipient="x@y.co", subject="hi", html=None, text_part="hi"
    )
    assert "Idempotency-Key" not in seen


def test_send_via_resend_reads_409_as_already_sent_not_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resend answers a replayed key + *different* payload with 409. A retry
    re-runs narration, which is LLM prose and never byte-identical, so this is
    the likely shape of a replay — and it means the email is already out."""
    calls = {"n": 0}

    def _post(_url: str, **kwargs: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(409, {"name": "invalid_idempotent_request"})

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", _post)

    with pytest.raises(deliver.AlreadySent):
        deliver.send_via_resend(
            sender="s <a@b.co>",
            recipient="x@y.co",
            subject="hi",
            html=None,
            text_part="hi",
            idempotency_key="brief-1:x@y.co",
        )
    assert calls["n"] == 1  # a 4xx is never retried


def test_deliver_derives_a_stable_key_from_brief_and_recipient(
    db_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stable across re-runs, or the replay it is meant to catch never matches."""
    _seed_brief(db_conn)
    keys: list[str | None] = []

    def _fake_send(**kwargs: Any) -> str:
        keys.append(kwargs["idempotency_key"])
        return "msg_abc"

    monkeypatch.setattr(deliver, "render_brief", lambda _id: ("<p>hi</p>", "hi"))
    monkeypatch.setattr(deliver, "send_via_resend", _fake_send)

    deliver.deliver_brief(
        db_conn, user_id=_TEST_USER_ID, session_date=_D, kind="close",
        recipient="x@y.co", sender="s <a@b.co>",
    )
    brief_id = db_conn.execute(
        text("SELECT id FROM briefs WHERE user_id = :u AND session_date = :d"),
        {"u": _TEST_USER_ID, "d": _D},
    ).scalar_one()
    assert keys == [f"{brief_id}:x@y.co"]


def test_deliver_records_a_replayed_send_instead_of_sending_again(
    db_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the retry must leave `deliveries` saying 'sent', so the
    *next* attempt short-circuits on the row rather than on the provider."""
    _seed_brief(db_conn)

    def _replayed(**kwargs: Any) -> str:
        raise deliver.AlreadySent("invalid_idempotent_request")

    monkeypatch.setattr(deliver, "render_brief", lambda _id: ("<p>hi</p>", "hi"))
    monkeypatch.setattr(deliver, "send_via_resend", _replayed)

    result = deliver.deliver_brief(
        db_conn, user_id=_TEST_USER_ID, session_date=_D, kind="close",
        recipient="x@y.co", sender="s <a@b.co>",
    )
    assert result.status == "skipped"

    status = db_conn.execute(
        text("SELECT status FROM deliveries WHERE recipient = 'x@y.co'")
    ).scalar_one()
    assert status == "sent"


# --- 409 is three different answers, and only one means "already sent" -------
#
# `invalid_idempotent_request` means the key was replayed with a changed payload:
# the mail is out. `concurrent_idempotent_requests` and `resource_locked` both
# mean "in progress, retry later" — Resend may never have sent anything. Reading
# those as sent would mark the brief delivered and ping the dead-man's switch
# *green* over a brief that never went out: worse than the duplicate the key
# exists to prevent, because a duplicate is at least visible. Anything we do not
# recognise therefore has to fail red, not green.


class _FakeUnparseable(_FakeResponse):
    def json(self) -> dict[str, Any]:
        raise ValueError("not json")


@pytest.mark.parametrize("name", ["concurrent_idempotent_requests", "resource_locked"])
def test_send_via_resend_does_not_read_a_retryable_409_as_already_sent(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(409, {"name": name}))
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    with pytest.raises(httpx.HTTPStatusError):
        deliver.send_via_resend(
            sender="s <a@b.co>", recipient="x@y.co", subject="hi", html=None,
            text_part="hi", idempotency_key="brief-1:x@y.co",
        )


def test_send_via_resend_fails_red_on_a_409_it_cannot_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeUnparseable(409, {}))
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    with pytest.raises(httpx.HTTPStatusError):
        deliver.send_via_resend(
            sender="s <a@b.co>", recipient="x@y.co", subject="hi", html=None,
            text_part="hi", idempotency_key="brief-1:x@y.co",
        )


def test_deliver_never_records_sent_when_resend_says_a_request_is_in_flight(
    db_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this guards: a brief marked delivered, the check green, and no
    email. The crash retry would then short-circuit on the row forever."""
    _seed_brief(db_conn)

    def _in_flight(**kwargs: Any) -> str:
        raise httpx.HTTPStatusError(
            "409", request=httpx.Request("POST", "x"), response=None  # type: ignore[arg-type]
        )

    monkeypatch.setattr(deliver, "render_brief", lambda _id: ("<p>hi</p>", "hi"))
    monkeypatch.setattr(deliver, "send_via_resend", _in_flight)

    with pytest.raises(httpx.HTTPStatusError):
        deliver.deliver_brief(
            db_conn, user_id=_TEST_USER_ID, session_date=_D, kind="close",
            recipient="x@y.co", sender="s <a@b.co>",
        )

    status = db_conn.execute(
        text("SELECT status FROM deliveries WHERE recipient = 'x@y.co'")
    ).scalar_one()
    assert status != "sent"
