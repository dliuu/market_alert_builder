"""M17 catalysts: insider (Form 4) and proposed-sale (Form 144) detectors.

Pure over the typed raw rows — no network, no clock, no database — so a
hand-built fixture per signal kind asserts exact output (the milestone's
golden-file DoD). Thresholds live in ``worker.constants``; severity 1-5 stays
internal to this module and is mapped to the BriefObject's ``tier`` by assembly,
because assembly decides suppression, never the renderer (D16).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker import calendar
from worker.constants import (
    CADENCE_BREAK_SEVERITY,
    CADENCE_MIN_PRIOR,
    CADENCE_SIZE_MULTIPLE,
    CATALYST_LOOKBACK_DAYS,
    CLEVEL_BUY_SEVERITY,
    CLUSTER_MIN_INSIDERS,
    CLUSTER_SESSIONS,
    CLUSTER_SEVERITY,
    LARGE_144_PCT,
    LARGE_144_SEVERITY,
    OUTSIZED_SALE_PCT,
    OUTSIZED_SALE_SEVERITY,
    PRE_EARNINGS_SESSIONS,
    PRE_EARNINGS_SEVERITY,
    STANDARD_144_SEVERITY,
    UNCONVERTED_144_DAYS,
    UNCONVERTED_144_SEVERITY,
)

# SEC Form 4 transaction codes, the vocabulary the ingest layer normalizes onto.
# P/S are the discretionary open-market trades that carry signal; M (option
# exercise) and F (shares withheld for tax) are mechanical and carry none.
_BUY = "P"
_SELL = "S"
# What ingest writes when the vendor's own type string maps to nothing we
# recognise (open question 2).
_AMBIGUOUS = "?"


@dataclass(frozen=True)
class InsiderTx:
    symbol: str
    insider_name: str
    insider_title: str | None
    transaction_date: date
    filing_date: date
    transaction_code: str
    shares: Decimal
    value_cents: int
    shares_after: Decimal | None
    row_id: int


@dataclass(frozen=True)
class ProposedSale:
    symbol: str
    insider_name: str
    filing_date: date
    shares_proposed: Decimal
    approx_sale_date: date | None
    row_id: int


@dataclass(frozen=True)
class CatalystSignal:
    source: str
    symbol: str
    kind: str
    ref_date: date
    severity: int
    detail: dict[str, object] = field(default_factory=dict)
    member_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReportingState:
    """What a *reader* has already been told about a signal. Keyed on the
    signal's natural identity, never on ``catalyst_signals.id``, because a
    rebuild reassigns that id (D30)."""

    report_count: int
    max_severity_seen: int


def render_tier(severity: int, state: ReportingState | None) -> str | None:
    """The BriefObject tier for a signal, or ``None`` to omit it entirely.

    Two independent effects compose. Severity sets the ceiling (§6): a
    severity-2 item is never more than one condensed line, however new it is.
    Report-once decay then demotes on repeat (§7): full, condensed, gone.

    Re-escalation overrides the decay when the signal has genuinely got worse
    than anything the reader has been shown — a 2-insider cluster becoming a
    4-insider cluster is new information, not a repeat.
    """
    ceiling = _SEVERITY_TIER.get(severity)
    if ceiling is None:
        return None
    if state is None or severity > state.max_severity_seen:
        return ceiling
    return _decay(ceiling, state.report_count)


# §6. Severity 1 is archival: stored and queryable, never rendered.
_SEVERITY_TIER: dict[int, str] = {5: "full", 4: "full", 3: "brief", 2: "brief"}

_DEMOTED: dict[str, str | None] = {"full": "brief", "brief": None}


def _decay(ceiling: str, report_count: int) -> str | None:
    if report_count <= 0:
        return ceiling
    if report_count == 1:
        return _DEMOTED[ceiling]
    return None


_CLEVEL = ("chief executive", "ceo", "chief financial", "cfo", "president")


def _is_clevel(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(marker in lowered for marker in _CLEVEL)


def _direction(code: str) -> str | None:
    """Buy, sell, or neither. ``M`` (option exercise) and ``F`` (shares withheld
    for tax) are mechanical and carry no signal, so they are neither. An
    unmapped code is treated as a sale but marks the signal ambiguous — open
    question 2: we neither silently include nor silently drop it."""
    if code == _BUY:
        return "buy"
    if code in (_SELL, _AMBIGUOUS):
        return "sell"
    return None


def _emit(t: InsiderTx, kind: str, severity: int, detail: dict[str, object]) -> CatalystSignal:
    """Build a signal, applying the ambiguous-code severity penalty (§5.1)."""
    if t.transaction_code == _AMBIGUOUS:
        severity = max(1, severity - 1)
        detail = {**detail, "ambiguous_code": True}
    return CatalystSignal(
        source="insider",
        symbol=t.symbol,
        kind=kind,
        ref_date=t.transaction_date,
        severity=severity,
        detail=detail,
        member_ids=(t.row_id,),
    )


def detect_insider(
    txs: list[InsiderTx], *, earnings: dict[str, date] | None = None
) -> list[CatalystSignal]:
    """Emit one signal per rule a filing set trips (§5.1). A filing set tripping
    several rules produces several rows; assembly collapses them per symbol and
    takes the max severity.

    ``earnings`` maps symbol to its next known earnings date, read from the
    ``events`` table. A symbol absent from it simply skips ``pre_earnings`` —
    the distance is never inferred.
    """
    earnings = earnings or {}
    signals: list[CatalystSignal] = []

    for t in txs:
        direction = _direction(t.transaction_code)
        if direction is None:
            continue

        if direction == "buy" and _is_clevel(t.insider_title):
            signals.append(_emit(t, "clevel_buy", CLEVEL_BUY_SEVERITY,
                                 {"insider_count": 1, "total_value_cents": t.value_cents}))

        if direction == "sell":
            signals.extend(_sale_rules(t, txs, earnings))

    signals.extend(_clusters(txs))
    return signals


def _sale_rules(
    t: InsiderTx, txs: list[InsiderTx], earnings: dict[str, date]
) -> list[CatalystSignal]:
    out: list[CatalystSignal] = []

    pct = _pct_of_holding(t)
    if pct is not None and pct >= OUTSIZED_SALE_PCT:
        out.append(_emit(t, "outsized_sale", OUTSIZED_SALE_SEVERITY,
                         {"pct_of_holding": pct, "total_value_cents": t.value_cents}))

    next_earnings = earnings.get(t.symbol)
    if next_earnings is not None:
        days = calendar.sessions_between(t.transaction_date, next_earnings)
        if 0 < days <= PRE_EARNINGS_SESSIONS:
            out.append(_emit(t, "pre_earnings", PRE_EARNINGS_SEVERITY,
                             {"days_to_event": days, "total_value_cents": t.value_cents}))

    if _breaks_cadence(t, txs):
        out.append(_emit(t, "cadence_break", CADENCE_BREAK_SEVERITY,
                         {"total_value_cents": t.value_cents}))

    return out


def _pct_of_holding(t: InsiderTx) -> Decimal | None:
    """Sold shares as a fraction of the pre-transaction holding. ``shares_after``
    is nullable; without it the denominator is unknown and the rule must not
    fire on an assumed one."""
    if t.shares_after is None:
        return None
    prior = t.shares_after + t.shares
    if prior <= 0:
        return None
    return t.shares / prior


def _breaks_cadence(t: InsiderTx, txs: list[InsiderTx]) -> bool:
    """A regular filer selling materially bigger than usual. Needs at least
    ``CADENCE_MIN_PRIOR`` earlier filings before "usual" means anything."""
    prior = [
        o for o in txs
        if o.insider_name == t.insider_name
        and o.symbol == t.symbol
        and o.transaction_date < t.transaction_date
        and _direction(o.transaction_code) == "sell"
    ]
    if len(prior) < CADENCE_MIN_PRIOR:
        return False
    sizes = sorted(o.shares for o in prior)
    typical = sizes[len(sizes) // 2]
    return typical > 0 and t.shares >= typical * CADENCE_SIZE_MULTIPLE


def detect_proposed(
    sales: list[ProposedSale],
    *,
    as_of: date,
    floats: dict[str, Decimal] | None = None,
    insider_txs: list[InsiderTx] | None = None,
) -> list[CatalystSignal]:
    """Form 144 signals (§5.2). A 144 is filed *before* execution, so this is
    the one genuinely forward-looking supply signal in the module — the output
    is framed that way.

    ``floats`` maps symbol to public float. A symbol absent from it cannot
    produce a ``large_144``: the magnitude rule must not fire on an assumed
    denominator (open question 4). The filing is still reported as
    ``standard_144`` — an unknown size is information, a missing row is not.
    """
    floats = floats or {}
    insider_txs = insider_txs or []
    out: list[CatalystSignal] = []

    for s in sales:
        pct = _pct_of_float(s, floats)
        # large/standard are a magnitude ladder on one event, not two rules, so
        # they are exclusive — unlike §5.1's independent insider rules.
        large = pct is not None and pct >= LARGE_144_PCT
        out.append(CatalystSignal(
            source="proposed",
            symbol=s.symbol,
            kind="large_144" if large else "standard_144",
            ref_date=s.filing_date,
            severity=LARGE_144_SEVERITY if large else STANDARD_144_SEVERITY,
            detail={"shares": s.shares_proposed, "pct_of_float": pct},
            member_ids=(s.row_id,),
        ))

        outstanding = (as_of - s.filing_date).days
        if outstanding > UNCONVERTED_144_DAYS and not _converted(s, insider_txs):
            out.append(CatalystSignal(
                source="proposed",
                symbol=s.symbol,
                kind="unconverted_144",
                ref_date=s.filing_date,
                severity=UNCONVERTED_144_SEVERITY,
                detail={"shares": s.shares_proposed, "pct_of_float": pct,
                        "days_outstanding": outstanding},
                member_ids=(s.row_id,),
            ))
    return out


def _pct_of_float(s: ProposedSale, floats: dict[str, Decimal]) -> Decimal | None:
    public_float = floats.get(s.symbol)
    if public_float is None or public_float <= 0:
        return None
    return s.shares_proposed / public_float


def _converted(s: ProposedSale, insider_txs: list[InsiderTx]) -> bool:
    """Whether a Form 4 sale by the same insider followed this filing. The
    tolerance is one-sided on purpose: a sale *before* the 144 is a different
    trade, not this one's execution."""
    return any(
        t.symbol == s.symbol
        and t.insider_name == s.insider_name
        and _direction(t.transaction_code) == "sell"
        and t.transaction_date >= s.filing_date
        for t in insider_txs
    )


def _clusters(txs: list[InsiderTx]) -> list[CatalystSignal]:
    """>=3 distinct insiders trading the same direction within
    ``CLUSTER_SESSIONS`` trading days. Filings, not people, is the easy misread:
    one insider filing three times is not a cluster."""
    out: list[CatalystSignal] = []
    by_group: dict[tuple[str, str], list[InsiderTx]] = {}
    for t in txs:
        direction = _direction(t.transaction_code)
        if direction is not None:
            by_group.setdefault((t.symbol, direction), []).append(t)

    for (symbol, _direction_key), group in sorted(by_group.items()):
        group = sorted(group, key=lambda t: (t.transaction_date, t.row_id))
        for i, anchor in enumerate(group):
            window = [
                t for t in group[i:]
                if calendar.sessions_between(anchor.transaction_date, t.transaction_date)
                <= CLUSTER_SESSIONS
            ]
            names = {t.insider_name for t in window}
            if len(names) < CLUSTER_MIN_INSIDERS:
                continue
            out.append(CatalystSignal(
                source="insider",
                symbol=symbol,
                kind="cluster",
                ref_date=window[-1].transaction_date,
                severity=CLUSTER_SEVERITY,
                detail={
                    "insider_count": len(names),
                    "total_value_cents": sum(t.value_cents for t in window),
                },
                member_ids=tuple(t.row_id for t in window),
            ))
            break  # one cluster per symbol/direction, anchored at its earliest filing
    return out


# --- Database layer -------------------------------------------------------


@dataclass(frozen=True)
class CatalystItem:
    """A signal as the brief will show it: the stored row plus the tier that
    this reader's decay state resolves it to."""

    source: str
    symbol: str
    kind: str
    ref_date: date
    severity: int
    tier: str
    detail: dict[str, object]


_INSERT_INSIDER = text("""
    INSERT INTO catalyst_insider_tx
        (symbol, insider_name, insider_title, transaction_date, filing_date,
         transaction_code, shares, value_cents, shares_after, natural_key)
    VALUES (:symbol, :insider_name, :insider_title, :transaction_date, :filing_date,
            :transaction_code, :shares, :value_cents, :shares_after, :natural_key)
    ON CONFLICT (natural_key) DO NOTHING
""")

_INSERT_SIGNAL = text("""
    INSERT INTO catalyst_signals
        (source, symbol, kind, ref_date, severity, detail, member_ids, model_version)
    VALUES (:source, :symbol, :kind, :ref_date, :severity, CAST(:detail AS jsonb),
            :member_ids, :model_version)
    ON CONFLICT (source, symbol, kind, ref_date, model_version) DO UPDATE
        SET severity = EXCLUDED.severity,
            detail = EXCLUDED.detail,
            member_ids = EXCLUDED.member_ids,
            computed_at = now()
""")

_READ_FEED = text("""
    SELECT s.source, s.symbol, s.kind, s.ref_date, s.severity, s.detail,
           r.report_count, r.max_severity_seen
      FROM catalyst_signals s
      LEFT JOIN catalyst_reporting_state r
             ON r.user_id = :user_id AND r.source = s.source AND r.symbol = s.symbol
            AND r.kind = s.kind AND r.ref_date = s.ref_date
     WHERE s.model_version = :model_version
       AND s.symbol = ANY(:symbols)
       AND s.ref_date <= :session_date
       AND s.ref_date > :horizon
     ORDER BY s.severity DESC, s.symbol, s.kind
""")

_MARK_REPORTED = text("""
    INSERT INTO catalyst_reporting_state
        (user_id, source, symbol, kind, ref_date,
         first_reported_at, last_reported_at, report_count, max_severity_seen)
    VALUES (:user_id, :source, :symbol, :kind, :ref_date, :now, :now, 1, :severity)
    ON CONFLICT (user_id, source, symbol, kind, ref_date) DO UPDATE
        SET report_count = catalyst_reporting_state.report_count + 1,
            last_reported_at = EXCLUDED.last_reported_at,
            max_severity_seen = GREATEST(
                catalyst_reporting_state.max_severity_seen, EXCLUDED.max_severity_seen
            )
""")

_READ_INSIDER = text("""
    SELECT id, symbol, insider_name, insider_title, transaction_date, filing_date,
           transaction_code, shares, value_cents, shares_after
      FROM catalyst_insider_tx
     ORDER BY symbol, transaction_date, id
""")

_READ_PROPOSED = text("""
    SELECT id, symbol, insider_name, filing_date, shares_proposed, approx_sale_date
      FROM catalyst_proposed_sales
     ORDER BY symbol, filing_date, id
""")


def natural_key(t: InsiderTx) -> str:
    """Deterministic dedup key. Open question 3: the vendor may expose a stable
    filing identifier, in which case ingest should prefer it and this becomes
    the fallback. A composite hash collapses an amendment whose every field is
    unchanged, which is the weaker behaviour the open question tracks."""
    raw = "|".join((
        t.symbol, t.insider_name, t.transaction_date.isoformat(),
        str(t.shares), str(t.value_cents), t.transaction_code,
    ))
    return hashlib.sha256(raw.encode()).hexdigest()


def store_insider_txs(conn: Connection, txs: list[InsiderTx]) -> int:
    """Replay typed insider rows. Idempotent on ``natural_key``, so a re-run
    inserts nothing and a *backdated* filing still lands — the watermark date
    alone would skip it."""
    stored = 0
    for t in txs:
        result = conn.execute(_INSERT_INSIDER, {
            "symbol": t.symbol, "insider_name": t.insider_name,
            "insider_title": t.insider_title, "transaction_date": t.transaction_date,
            "filing_date": t.filing_date, "transaction_code": t.transaction_code,
            "shares": t.shares, "value_cents": t.value_cents,
            "shares_after": t.shares_after, "natural_key": natural_key(t),
        })
        stored += result.rowcount or 0
    return stored


def store_signals(conn: Connection, signals: list[CatalystSignal], *, model_version: str) -> int:
    stored = 0
    for s in signals:
        conn.execute(_INSERT_SIGNAL, {
            "source": s.source, "symbol": s.symbol, "kind": s.kind,
            "ref_date": s.ref_date, "severity": s.severity,
            "detail": json.dumps(s.detail, default=str),
            "member_ids": list(s.member_ids), "model_version": model_version,
        })
        stored += 1
    return stored


def read_insider_txs(conn: Connection) -> list[InsiderTx]:
    return [
        InsiderTx(
            symbol=r["symbol"], insider_name=r["insider_name"],
            insider_title=r["insider_title"], transaction_date=r["transaction_date"],
            filing_date=r["filing_date"], transaction_code=r["transaction_code"],
            shares=Decimal(str(r["shares"] or 0)), value_cents=int(r["value_cents"] or 0),
            shares_after=None if r["shares_after"] is None else Decimal(str(r["shares_after"])),
            row_id=r["id"],
        )
        for r in conn.execute(_READ_INSIDER).mappings().all()
    ]


def read_proposed_sales(conn: Connection) -> list[ProposedSale]:
    return [
        ProposedSale(
            symbol=r["symbol"], insider_name=r["insider_name"],
            filing_date=r["filing_date"],
            shares_proposed=Decimal(str(r["shares_proposed"] or 0)),
            approx_sale_date=r["approx_sale_date"], row_id=r["id"],
        )
        for r in conn.execute(_READ_PROPOSED).mappings().all()
    ]


def rebuild_signals(
    conn: Connection,
    *,
    model_version: str,
    as_of: date | None = None,
    earnings: dict[str, date] | None = None,
    floats: dict[str, Decimal] | None = None,
) -> int:
    """Drop and recompute every signal for ``model_version`` from the raw
    tables. **Zero API calls** — the detectors read only stored rows.

    ``catalyst_reporting_state`` is deliberately untouched. If it lived on the
    signal row, this function would wipe it and every stale cluster would
    resurface at full volume on the next brief (D30).
    """
    conn.execute(
        text("DELETE FROM catalyst_signals WHERE model_version = :mv"),
        {"mv": model_version},
    )
    txs = read_insider_txs(conn)
    sales = read_proposed_sales(conn)
    signals = detect_insider(txs, earnings=earnings)
    if sales:
        signals += detect_proposed(
            sales, as_of=as_of or date.today(), floats=floats, insider_txs=txs
        )
    return store_signals(conn, signals, model_version=model_version)


def read_catalysts(
    conn: Connection,
    user_id: str,
    symbols: list[str],
    session_date: date,
    *,
    model_version: str,
    lookback_days: int = CATALYST_LOOKBACK_DAYS,
) -> list[CatalystItem]:
    """The feed for one reader, one session — the source spec's ``catalyst_feed``
    view as a query (D30), with each row's tier resolved through that reader's
    decay state. Rows that decay to nothing are dropped here, so assembly never
    sees them."""
    if not symbols:
        return []
    rows = conn.execute(_READ_FEED, {
        "user_id": user_id, "symbols": symbols, "session_date": session_date,
        "horizon": session_date - timedelta(days=lookback_days),
        "model_version": model_version,
    }).mappings().all()

    out: list[CatalystItem] = []
    for r in rows:
        state = (
            None if r["report_count"] is None
            else ReportingState(int(r["report_count"]), int(r["max_severity_seen"]))
        )
        tier = render_tier(int(r["severity"]), state)
        if tier is None:
            continue
        out.append(CatalystItem(
            source=r["source"], symbol=r["symbol"], kind=r["kind"],
            ref_date=r["ref_date"], severity=int(r["severity"]), tier=tier,
            detail=dict(r["detail"] or {}),
        ))
    return out


def mark_reported(
    conn: Connection, user_id: str, items: list[CatalystItem], *, now: datetime
) -> None:
    """Advance the decay curve. Called only for items that actually reached a
    reader — a brief that doesn't send must not consume anyone's attention."""
    for i in items:
        conn.execute(_MARK_REPORTED, {
            "user_id": user_id, "source": i.source, "symbol": i.symbol,
            "kind": i.kind, "ref_date": i.ref_date, "now": now, "severity": i.severity,
        })
