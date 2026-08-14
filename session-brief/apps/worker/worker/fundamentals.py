"""M18 stage ②: SEC EDGAR `companyfacts` → the `fundamentals` table.

`normalize_companyfacts` is pure — payload in, typed rows out, no network and no
clock — so the concept mapping is testable against a stored fixture and a
mapping bug is fixed by re-normalizing rather than re-fetching several MB per
symbol (the M2 replay property, invariant 5).

Three properties of the real API shape drive the design, all verified against a
live response rather than assumed:

- **`filed` trails `end`, sometimes by months.** AAPL's `EntityPublicFloat` for
  period end 2025-03-28 was filed 2025-10-31. `as_of` is therefore the *filing*
  date; keying on period end would make every consumer look ahead (D31).
- **One concept mixes duration lengths.** AAPL's `NetIncomeLoss` carries 3-, 6-,
  9- and 12-month spans in the same list. A 9-month year-to-date figure summed
  as if quarterly would triple-count, so only quarter-ish and year-ish spans are
  kept and they are tagged apart.
- **8-K earnings releases restate figures the 10-Q also reports.** Taking both
  would double a quarter, so only periodic forms are read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection


class EdgarProvider(Protocol):
    """The fundamentals seam. Its own protocol rather than a widened
    `MarketDataProvider` — one protocol per capability (D28): an EOD price
    vendor structurally cannot serve an XBRL filing."""

    def cik_for(self, symbol: str) -> str | None: ...
    def company_facts(self, cik: str) -> dict[str, Any]: ...
    def domicile(self, cik: str) -> str | None: ...

# The five concepts the four consumers need, and nothing else — this is not a
# general financials store (D31). `dei` concepts are cover-page facts.
_SHARES_OUT = "EntityCommonStockSharesOutstanding"
# Fallback for registrants that don't tag the dei cover-page count. Note this
# does *not* rescue multi-class registrants: companyfacts drops dimensional
# facts, so a company reporting shares per share class exposes no total here.
# A weighted-average count is a different measure and is deliberately not
# substituted — see the coverage note in the M18 spec.
_SHARES_OUT_GAAP = "CommonStockSharesOutstanding"
_PUBLIC_FLOAT = "EntityPublicFloat"
_NET_INCOME = "NetIncomeLoss"
_CASH = "CashAndCashEquivalentsAtCarryingValue"
_OPERATING_CF = "NetCashProvidedByUsedInOperatingActivities"

# Periodic reports only. An 8-K earnings release repeats the quarter the 10-Q
# will report, under its own accession — counting both doubles the period.
_PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "40-F"})

# A duration fact is kept only if it spans about a quarter or about a year.
# Everything between is year-to-date and belongs to no single period.
_QUARTER_DAYS = (80, 100)
_YEAR_DAYS = (350, 380)

_CENTS = Decimal(100)


@dataclass(frozen=True)
class FundamentalRow:
    """One filing's worth of facts, as `fundamentals` stores them."""

    symbol: str
    as_of: date            # the SEC filing date — never the period end (D31)
    period_end: date
    fiscal_period: str | None
    cik: str | None
    shares_out: int | None = None
    public_float_cents: int | None = None
    net_income_cents: int | None = None
    cash_cents: int | None = None
    quarterly_burn_cents: int | None = None


def normalize_companyfacts(payload: dict[str, Any], *, symbol: str) -> list[FundamentalRow]:
    """Group a companyfacts response into one row per periodic filing.

    Facts from a single filing share an accession number, so that is the
    grouping key: a 10-Q's net income, cash and share count land on one row.
    Rows come back ordered by filing date, oldest first.
    """
    cik = _cik(payload)
    facts = payload.get("facts") or {}
    dei = facts.get("dei") or {}
    gaap = facts.get("us-gaap") or {}

    # Grouped by accession alone — one row per filing. It has to be: the table
    # is keyed (symbol, as_of), so two rows sharing a filing date would collide.
    # That matters because one filing reports several measurement dates: a
    # 10-Q's cover-page share count is dated *after* the quarter end its
    # financial statements cover. `period_end` therefore prefers the us-gaap
    # facts, which define the reporting period; the cover count rides along.
    gathered: dict[str, dict[str, Any]] = {}

    for concept, source, field, kind, defines_period in (
        # us-gaap first so the dei cover-page count overwrites it when both
        # exist — the cover count is the authoritative total.
        (_SHARES_OUT_GAAP, gaap, "shares_out", "instant", False),
        (_SHARES_OUT, dei, "shares_out", "instant", False),
        (_PUBLIC_FLOAT, dei, "public_float_cents", "instant", False),
        (_NET_INCOME, gaap, "net_income_cents", "duration", True),
        (_CASH, gaap, "cash_cents", "instant", True),
        (_OPERATING_CF, gaap, "quarterly_burn_cents", "duration", True),
    ):
        for o in _observations(source.get(concept)):
            parsed = _parse(o, kind)
            if parsed is None:
                continue
            end, fiscal_period = parsed
            row = gathered.setdefault(str(o["accn"]), {
                "filed": date.fromisoformat(str(o["filed"])),
                "fiscal_period": fiscal_period,
                "period_end": end,
                "period_from_gaap": defines_period,
            })
            row[field] = _value(o["val"], field)
            # A us-gaap end supersedes a dei one; among us-gaap ends, the
            # earliest is the statement period rather than a later cover date.
            if defines_period and (
                not row["period_from_gaap"] or end < row["period_end"]
            ):
                row["period_end"] = end
                row["period_from_gaap"] = True
                row["fiscal_period"] = fiscal_period

    rows = [
        FundamentalRow(
            symbol=symbol,
            as_of=data["filed"],
            period_end=data["period_end"],
            fiscal_period=data["fiscal_period"],
            cik=cik,
            shares_out=data.get("shares_out"),
            public_float_cents=data.get("public_float_cents"),
            net_income_cents=data.get("net_income_cents"),
            cash_cents=data.get("cash_cents"),
            quarterly_burn_cents=data.get("quarterly_burn_cents"),
        )
        for data in gathered.values()
    ]
    rows.sort(key=lambda r: (r.as_of, r.period_end))
    return rows


def _observations(concept: Any) -> list[dict[str, Any]]:
    """Flatten a concept's unit buckets. `shares` and `USD` are the only units
    these five concepts use; anything else is a different measure wearing the
    same name and is skipped."""
    if not isinstance(concept, dict):
        return []
    units = concept.get("units") or {}
    out: list[dict[str, Any]] = []
    for unit in ("USD", "shares"):
        out.extend(units.get(unit) or [])
    return out


def _parse(o: dict[str, Any], kind: str) -> tuple[date, str | None] | None:
    """Validate one observation and return `(period_end, fiscal_period)`, or
    None if it should not be stored."""
    if str(o.get("form")) not in _PERIODIC_FORMS:
        return None
    if not o.get("filed") or not o.get("end") or o.get("val") is None:
        return None

    end = date.fromisoformat(str(o["end"]))
    fiscal_period = str(o["fp"]) if o.get("fp") else None

    if kind == "instant":
        return end, fiscal_period

    start_raw = o.get("start")
    if not start_raw:
        return None
    span = (end - date.fromisoformat(str(start_raw))).days
    if _QUARTER_DAYS[0] <= span <= _QUARTER_DAYS[1]:
        return end, fiscal_period
    if _YEAR_DAYS[0] <= span <= _YEAR_DAYS[1]:
        return end, "FY"
    return None  # year-to-date: belongs to no single period


def _value(raw: Any, field: str) -> int:
    """Money to integer cents via Decimal, never float (invariant). Share counts
    are quantities and stay whole.

    `quarterly_burn_cents` is **negated** operating cash flow. The column is a
    burn, not a cash flow, and `flags.runway_quarters` divides by it and bails
    when the mean is <= 0 — so storing raw OCF would invert the flag exactly:
    a genuine cash-burner (negative OCF) would report no runway, while a cash
    generator would report a meaningless one.
    """
    value = Decimal(str(raw))
    if field == "shares_out":
        return int(value)
    if field == "quarterly_burn_cents":
        value = -value
    return int((value * _CENTS).to_integral_value())


def _cik(payload: dict[str, Any]) -> str | None:
    raw = payload.get("cik")
    return None if raw is None else str(raw).zfill(10)


# --- Database layer -------------------------------------------------------


_INSERT_PAYLOAD = text("""
    INSERT INTO raw_payloads (source, endpoint, symbol, as_of, body)
    VALUES ('edgar', 'companyfacts', :symbol, :as_of, CAST(:body AS jsonb))
    ON CONFLICT (source, endpoint, symbol, as_of) DO UPDATE
        SET body = EXCLUDED.body, fetched_at = now()
""")

_READ_PAYLOAD = text("""
    SELECT DISTINCT ON (symbol) symbol, body
      FROM raw_payloads
     WHERE source = 'edgar' AND endpoint = 'companyfacts' AND symbol = ANY(:symbols)
     ORDER BY symbol, as_of DESC
""")

_UPSERT = text("""
    INSERT INTO fundamentals
        (symbol, as_of, period_end, fiscal_period, cik, source, domicile,
         shares_out, public_float_cents, net_income_cents, cash_cents,
         quarterly_burn_cents)
    VALUES (:symbol, :as_of, :period_end, :fiscal_period, :cik, 'edgar', :domicile,
            :shares_out, :public_float_cents, :net_income_cents, :cash_cents,
            :quarterly_burn_cents)
    ON CONFLICT (symbol, as_of, period_end) DO UPDATE
        SET fiscal_period      = EXCLUDED.fiscal_period,
            cik                = EXCLUDED.cik,
            domicile           = COALESCE(EXCLUDED.domicile, fundamentals.domicile),
            shares_out         = EXCLUDED.shares_out,
            public_float_cents = EXCLUDED.public_float_cents,
            net_income_cents   = EXCLUDED.net_income_cents,
            cash_cents         = EXCLUDED.cash_cents,
            quarterly_burn_cents = EXCLUDED.quarterly_burn_cents
""")


def ingest_fundamentals(
    conn: Connection,
    client: EdgarProvider,
    symbols: list[str],
    *,
    as_of: date,
) -> dict[str, Any]:
    """Fetch, store verbatim, and replay into `fundamentals`.

    A symbol EDGAR doesn't know is **skipped and reported**, never guessed into
    a CIK — a wrong CIK silently ingests another company's filings, which is a
    far worse failure than a missing row.
    """
    stored = 0
    skipped: list[str] = []

    for symbol in symbols:
        cik = client.cik_for(symbol)
        if cik is None:
            skipped.append(symbol)
            continue

        payload = client.company_facts(cik)
        conn.execute(_INSERT_PAYLOAD, {
            "symbol": symbol, "as_of": as_of,
            "body": json.dumps(payload, default=str),
        })
        stored += _store(conn, symbol, payload, domicile=client.domicile(cik))

    return {"stored": stored, "skipped": skipped}


def replay_fundamentals(conn: Connection, symbols: list[str]) -> int:
    """Rebuild the typed rows from the stored payloads. **Zero network calls** —
    a concept-mapping fix costs a replay, not several MB per symbol refetched
    (the M2 property, invariant 5). Domicile is preserved by the upsert's
    COALESCE, since it comes from a different endpoint."""
    rows = conn.execute(_READ_PAYLOAD, {"symbols": symbols}).mappings().all()
    return sum(
        _store(conn, r["symbol"], _as_dict(r["body"]), domicile=None) for r in rows
    )


def _store(
    conn: Connection, symbol: str, payload: dict[str, Any], *, domicile: str | None
) -> int:
    written = 0
    for row in normalize_companyfacts(payload, symbol=symbol):
        conn.execute(_UPSERT, {
            "symbol": row.symbol, "as_of": row.as_of, "period_end": row.period_end,
            "fiscal_period": row.fiscal_period, "cik": row.cik, "domicile": domicile,
            "shares_out": row.shares_out, "public_float_cents": row.public_float_cents,
            "net_income_cents": row.net_income_cents, "cash_cents": row.cash_cents,
            "quarterly_burn_cents": row.quarterly_burn_cents,
        })
        written += 1
    return written


def _as_dict(body: Any) -> dict[str, Any]:
    """`raw_payloads.body` is jsonb; the driver may hand back a dict or a str."""
    if isinstance(body, dict):
        return body
    parsed = json.loads(str(body), parse_float=Decimal)
    return parsed if isinstance(parsed, dict) else {}
