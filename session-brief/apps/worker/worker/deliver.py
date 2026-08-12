"""Stage ⑦ — deliver a rendered brief through Resend (M9).

The template lives only in the web app (docs/02 #4): the worker POSTs the brief
id to ``/api/render/:brief_id`` and receives ``{ html, text }`` back, then hands
that to Resend. Delivery is idempotent on ``(brief_id, recipient)`` — a retry
re-selects the ``deliveries`` row rather than mailing twice (invariant #6).

Failure policy (docs/06): render endpoint down → send a minimal plaintext-only
part; Resend 5xx → retry 3× with backoff, then mark the row ``failed``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from contracts.brief import BriefObject
from worker import config

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass
class DeliveryResult:
    status: str  # sent | skipped | failed | dry_run
    recipient: str
    provider_msg_id: str | None = None
    html_bytes: int | None = None
    detail: str | None = None


@dataclass
class _BriefRow:
    id: str
    subject: str
    obj: BriefObject
    session_date: date
    kind: str


_READ_BRIEF = text("""
    SELECT id, kind, session_date, body
    FROM briefs
    WHERE user_id = :user_id AND session_date = :session_date AND kind = :kind
""")

_UPSERT_PENDING = text("""
    INSERT INTO deliveries (user_id, brief_id, recipient, status)
    VALUES (:user_id, :brief_id, :recipient, 'pending')
    ON CONFLICT (brief_id, recipient) DO UPDATE
        SET status = 'pending', error = NULL, updated_at = now()
""")

_MARK_SENT = text("""
    UPDATE deliveries
        SET status = 'sent', provider_msg_id = :mid, error = NULL, updated_at = now()
    WHERE brief_id = :brief_id AND recipient = :recipient
""")

_MARK_FAILED = text("""
    UPDATE deliveries
        SET status = 'failed', error = :error, updated_at = now()
    WHERE brief_id = :brief_id AND recipient = :recipient
""")


def resolve_brief(
    conn: Connection, user_id: str, session_date: date, kind: str
) -> _BriefRow | None:
    row = conn.execute(
        _READ_BRIEF, {"user_id": user_id, "session_date": session_date, "kind": kind}
    ).mappings().first()
    if row is None:
        return None
    obj = BriefObject.model_validate(row["body"])
    return _BriefRow(
        id=str(row["id"]),
        subject=obj.subject,
        obj=obj,
        session_date=row["session_date"],
        kind=row["kind"],
    )


def render_brief(brief_id: str) -> tuple[str, str]:
    """Call the web render endpoint. Raises ``httpx.HTTPError`` if it's down —
    the caller degrades to a plaintext-only send."""
    resp = httpx.post(
        f"{config.WEB_RENDER_URL}/api/render/{brief_id}",
        headers={"Authorization": f"Bearer {config.RENDER_SHARED_SECRET}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["html"], data["text"]


def _fallback_text(obj: BriefObject, session_date: date, kind: str) -> str:
    """Minimal plaintext for the endpoint-down path — a link home, not a second
    renderer. A brief that arrives thin beats a brief that didn't arrive."""
    url = f"{config.WEB_RENDER_URL}/briefs/{session_date}-{kind}"
    lines = [obj.subject, ""]
    if obj.one_thing:
        lines += [obj.one_thing, ""]
    lines.append(f"Read the full brief: {url}")
    return "\n".join(lines)


def send_via_resend(
    *, sender: str, recipient: str, subject: str, html: str | None, text_part: str
) -> str:
    """POST to Resend, retrying 5xx up to 3× with backoff. Returns the provider
    message id. 4xx raises immediately (a bad payload won't fix itself)."""
    payload: dict[str, object] = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": text_part,
        "headers": {
            "List-Unsubscribe": f"<{config.WEB_RENDER_URL}/unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    if html:
        payload["html"] = html

    headers = {"Authorization": f"Bearer {config.RESEND_API_KEY}"}
    resp: httpx.Response | None = None
    for attempt in range(3):
        resp = httpx.post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=30.0)
        if resp.status_code < 500:
            resp.raise_for_status()  # 2xx returns; 4xx raises, no retry
            msg_id = resp.json()["id"]
            return str(msg_id)
        time.sleep(2**attempt)  # 1s, 2s, 4s
    assert resp is not None
    resp.raise_for_status()  # exhausted retries on 5xx
    raise RuntimeError("unreachable")  # pragma: no cover


def deliver_brief(
    conn: Connection,
    *,
    user_id: str,
    session_date: date,
    kind: str,
    recipient: str,
    sender: str,
    dry_run: bool = False,
) -> DeliveryResult:
    brief = resolve_brief(conn, user_id, session_date, kind)
    if brief is None:
        raise LookupError(f"no {kind} brief for {user_id} on {session_date}")

    # Render up front so a dry run reports the real size; endpoint down ⇒ fall back.
    fell_back = False
    try:
        html: str | None
        html, text_part = render_brief(brief.id)
    except httpx.HTTPError:
        html = None
        text_part = _fallback_text(brief.obj, session_date, kind)
        fell_back = True

    html_bytes = len(html.encode("utf-8")) if html is not None else None
    detail = "fallback text-only (render endpoint down)" if fell_back else None

    if dry_run:
        return DeliveryResult(
            status="dry_run", recipient=recipient, html_bytes=html_bytes, detail=detail
        )

    # Idempotency: a row already 'sent' means a prior run reached Resend. Skip.
    existing = conn.execute(
        text("SELECT status FROM deliveries WHERE brief_id = :b AND recipient = :r"),
        {"b": brief.id, "r": recipient},
    ).mappings().first()
    if existing is not None and existing["status"] == "sent":
        return DeliveryResult(status="skipped", recipient=recipient, detail="already sent")

    conn.execute(
        _UPSERT_PENDING, {"user_id": user_id, "brief_id": brief.id, "recipient": recipient}
    )

    try:
        msg_id = send_via_resend(
            sender=sender,
            recipient=recipient,
            subject=brief.subject,
            html=html,
            text_part=text_part,
        )
    except Exception as exc:
        conn.execute(
            _MARK_FAILED, {"brief_id": brief.id, "recipient": recipient, "error": str(exc)}
        )
        raise

    conn.execute(_MARK_SENT, {"brief_id": brief.id, "recipient": recipient, "mid": msg_id})
    return DeliveryResult(
        status="sent",
        recipient=recipient,
        provider_msg_id=msg_id,
        html_bytes=html_bytes,
        detail=detail,
    )
