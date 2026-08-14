"""M17: the catalysts feed as a BriefObject section.

Pure — a list of ``CatalystItem`` in, a section dict out. Kept out of
``assemble.py`` for the same reason ``assemble_open.py`` is: the close
assembler's P&L path is load-bearing (invariant 3) and does not need to grow.

Two responsibilities live here and nowhere downstream. **Suppression**: a name
with no catalysts is folded into one roll-up line rather than rendered as an
empty heading (D16 — assembly decides what to hide, never the renderer).
**Types**: ``detail`` arrives from ``jsonb``, which round-trips numbers as
strings or ``Decimal``, and the contract wants ``int``/``float``.
"""

from __future__ import annotations

from decimal import Decimal

from worker.catalysts import CatalystItem


def catalysts_section(items: list[CatalystItem], *, held: list[str]) -> dict[str, object]:
    """Build the section. One row per signal, ordered by severity descending —
    the renderer groups them by symbol, it does not re-rank them."""
    rows = [_row(i) for i in sorted(items, key=lambda i: (-i.severity, i.symbol, i.kind))]
    quiet = sorted(set(held) - {i.symbol for i in items})
    return {
        "id": "catalysts",
        "tier": "full" if rows else "suppressed",
        "note": f"No catalysts: {', '.join(quiet)}" if quiet else None,
        "rows": rows,
    }


def _row(i: CatalystItem) -> dict[str, object]:
    d = i.detail
    return {
        "symbol": i.symbol,
        "tier": i.tier,
        "source": i.source,
        "kind": i.kind,
        "ref_date": i.ref_date.isoformat(),
        "insider_count": _int(d.get("insider_count")),
        "value_cents": _int(d.get("total_value_cents")),
        "pct_of_holding": _float(d.get("pct_of_holding")),
        "shares": _float(d.get("shares")),
        "pct_of_float": _float(d.get("pct_of_float")),
        "days_to_event": _int(d.get("days_to_event")),
        "days_outstanding": _int(d.get("days_outstanding")),
        "ambiguous_code": bool(d["ambiguous_code"]) if d.get("ambiguous_code") else None,
        "why": None,  # narration, and the only free text on the row (D2)
    }


def _int(value: object) -> int | None:
    if value is None:
        return None
    return int(Decimal(str(value)))


def _float(value: object) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)))
