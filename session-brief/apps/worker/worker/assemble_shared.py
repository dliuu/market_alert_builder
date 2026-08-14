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

from worker.claims import Claim, ResolvedClaim


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
