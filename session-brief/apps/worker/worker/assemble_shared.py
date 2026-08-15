"""Helpers both assemblers use (M14).

The close assembler (``assemble.py``) is P&L-centric and the open one
(``assemble_open.py``) has no P&L at all, so they are deliberately separate
functions rather than one branching monster. What they genuinely share is small
and mechanical: turning exact ``Fraction``s into the contract's display types,
and the claim dicts. Extracting it keeps the duplication out without coupling
the two paths.

Nothing here knows what a brief *is* — no section building, no ordering.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from worker.claims import Claim, ResolvedClaim

if TYPE_CHECKING:
    from contracts.brief import BriefObject


def round_bps(value: Fraction | None) -> int | None:
    """Round an exact bps ``Fraction`` to the contract's integer. The exact sum
    identity (Σ contribution_bps == day_bps) lives in stage ③ over Fractions;
    the rounded integers here are for display and need not re-sum (D15)."""
    if value is None:
        return None
    return int(
        (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def session_label(session_date: date) -> str:
    """"Tue Aug 11" — the date fragment both subject lines use."""
    return f"{session_date:%a} {session_date:%b} {session_date.day}"


_CURRENCY_SYMBOLS = {"USD": "$", "CNY": "¥"}


def signed_money(minor_units: int, currency: str = "USD") -> str:
    """Signed, thousands-separated money for a subject line — generalizes
    ``assemble._signed_dollars`` across currencies. ``minor_units`` is cents;
    reproduces ``_signed_dollars``'s sign/formatting digit-for-digit for USD."""
    symbol = _CURRENCY_SYMBOLS.get(currency)
    if symbol is None:
        raise ValueError(f"unknown currency: {currency}")
    amount = minor_units / 100
    sign = "+" if amount >= 0 else "-"
    return f"{sign}{symbol}{abs(amount):,.2f}"


def to_contract_json(obj: BriefObject) -> dict[str, object]:
    """The contract-compliant JSON dict for a ``BriefObject`` — what actually
    gets stored, and what ``test_contract_schema.py`` validates.

    Codegen types the optional, non-nullable ``currency`` as ``Currency | None
    = None`` (docs/04's documented Pydantic-vs-schema gap: a non-required
    schema property still gets a ``None`` default), so a plain ``model_dump``
    emits an explicit ``"currency": null`` the schema's own enum rejects.
    Strip it when unset — the contract's rule is "absent means USD," not
    "null means USD" (v7, CN-M1)."""
    body = obj.model_dump(mode="json")
    if body.get("currency") is None:
        body.pop("currency", None)
    return body


def claim_dict(claim: Claim, user_id: str, session_date: date, kind: str) -> dict[str, object]:
    # Emitted claims have no DB id yet; a deterministic slug identifies them.
    return {
        "id": f"{user_id}-{session_date.isoformat()}-{kind}-{claim.symbol}-{claim.claim_type}",
        "symbol": claim.symbol,
        "type": claim.claim_type,
        "direction": claim.direction,
        "horizon_sessions": claim.horizon_sessions,
        "outcome": None,
    }


def resolved_dict(resolved: ResolvedClaim) -> dict[str, object]:
    return {
        "id": resolved.id,
        "symbol": resolved.symbol,
        "type": resolved.claim_type,
        "direction": resolved.direction,
        "horizon_sessions": resolved.horizon_sessions,
        "outcome": resolved.outcome,
    }
