"""Stage ④ flags: position risk + the correlation flag (M7, docs/05).

Two mechanisms, both silent by default:

- **Position risk** — per held name, fires on low runway, high dilution, an
  earnings print or a supply (lockup) event soon, or high short interest. Not
  rate-limited; these are intermittent and event-shaped.
- **Correlation flag** — the exposure mechanism: any single name > 20% of book,
  any sector > 50%, or mean 20d pairwise correlation > 0.75. Hard-capped at one
  email mention per week (docs/05), enforced by ``flags.last_seen``.

Like ``claims.py``/``tape.py``, the thresholds are a **pure core** (trivially
unit-testable — the DoD's "thresholds fire on a synthetic fixture") and the
reads/rate-limit live in a **DB layer**, kept out of the M3-certified compute.

M7 seeds ``fundamentals``/``events`` synthetically; real earnings/fundamentals
ingest is deferred (docs/02 TBD source gaps). ``short_interest`` has no live
source in v1 (docs/02 skips it) — its threshold exists and fires on synthetic
input, but nothing feeds it in normal operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.compute import ComputeResult

# Thresholds (docs/05).
_RUNWAY_MIN_Q = Fraction(6)
_DILUTION_MAX = Fraction(15, 100)
_SHORT_INTEREST_MAX = Fraction(20, 100)
_NAME_WEIGHT_MAX = Fraction(20, 100)
_SECTOR_WEIGHT_MAX = Fraction(50, 100)
_CORR_MAX = 0.75

# "Earnings within 5 sessions" / "supply event within 7 days" (docs/05). Both are
# thresholded in *calendar* days here: a future date can't use bars_daily as the
# session clock (the claims trick) and exchange_calendars isn't a worker dep yet,
# so ~5 sessions is approximated as ≤ 7 days. Swapping to true session-counting
# when the calendar lands (M10) is a one-line change in the DB layer.
_EARNINGS_WINDOW_DAYS = 7
_SUPPLY_WINDOW_DAYS = 7

# Rolling correlation window (docs/03 corr_20d). Require the full window so the
# figure means something.
_CORR_WINDOW = 20

# Flag types the once-a-week email cap applies to (docs/05: the correlation flag,
# which includes single-name and sector concentration). Position-risk types are
# not capped.
_WEEKLY_CAPPED = {"concentration", "correlation"}
_RATE_LIMIT_DAYS = 7


@dataclass(frozen=True)
class FlagCandidate:
    type: str
    severity: str
    symbol: str | None
    sector_id: str | None
    value: float | None
    text_key: str


@dataclass(frozen=True)
class _Fundamental:
    as_of: date
    cash_cents: int | None
    quarterly_burn_cents: int | None
    shares_out: int | None
    next_earnings_date: date | None


# --- Numeric helpers (pure) -----------------------------------------------


def runway_quarters(cash_cents: int, burns_cents: list[int]) -> Fraction | None:
    """Cash divided by mean quarterly burn (docs/03 cash_runway_q). ``None`` when
    there's no burn to divide by."""
    if not burns_cents:
        return None
    mean_burn = Fraction(sum(burns_cents), len(burns_cents))
    if mean_burn <= 0:
        return None
    return Fraction(cash_cents) / mean_burn


def dilution_yoy(shares_now: int, shares_year_ago: int) -> Fraction | None:
    """Year-over-year share-count growth (docs/03 dilution_yoy). ``None`` without
    a prior base."""
    if shares_year_ago <= 0:
        return None
    return Fraction(shares_now, shares_year_ago) - 1


def mean_pairwise_corr(returns_by_symbol: dict[str, list[float]]) -> float | None:
    """Mean Pearson correlation over every pair of the given return series
    (docs/05). Series are assumed equal-length; a series with zero variance
    (flat) can't correlate and is dropped. ``None`` with fewer than two usable
    series. Float — correlation needs ``sqrt`` and is off the money path."""
    symbols = sorted(returns_by_symbol)
    corrs: list[float] = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            c = _pearson(returns_by_symbol[a], returns_by_symbol[b])
            if c is not None:
                corrs.append(c)
    if not corrs:
        return None
    return sum(corrs) / len(corrs)


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((a - mean_x) ** 2 for a in x)
    syy = sum((b - mean_y) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:  # a flat series has no correlation
        return None
    sxy = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    return float(sxy / (sxx**0.5 * syy**0.5))


# --- Threshold predicates (pure) ------------------------------------------


def runway_flag(symbol: str, runway_q: Fraction | None) -> FlagCandidate | None:
    if runway_q is None or runway_q >= _RUNWAY_MIN_Q:
        return None
    return FlagCandidate("runway", "warn", symbol, None, float(runway_q), "runway_low")


def dilution_flag(symbol: str, dilution: Fraction | None) -> FlagCandidate | None:
    if dilution is None or dilution <= _DILUTION_MAX:
        return None
    return FlagCandidate("dilution", "warn", symbol, None, float(dilution), "dilution_high")


def earnings_soon_flag(symbol: str, days_to_earnings: int | None) -> FlagCandidate | None:
    if days_to_earnings is None or not 0 <= days_to_earnings <= _EARNINGS_WINDOW_DAYS:
        return None
    return FlagCandidate("earnings_soon", "info", symbol, None, float(days_to_earnings),
                         "earnings_soon")


def supply_event_flag(symbol: str, days_to_supply: int | None) -> FlagCandidate | None:
    if days_to_supply is None or not 0 <= days_to_supply <= _SUPPLY_WINDOW_DAYS:
        return None
    return FlagCandidate("supply_event", "warn", symbol, None, float(days_to_supply),
                         "supply_event_soon")


def short_interest_flag(symbol: str, short_interest: Fraction | None) -> FlagCandidate | None:
    if short_interest is None or short_interest <= _SHORT_INTEREST_MAX:
        return None
    return FlagCandidate("short_interest", "warn", symbol, None, float(short_interest),
                         "short_interest_high")


def concentration_flags(
    name_weights: dict[str, Fraction], sector_weights: dict[str, Fraction]
) -> list[FlagCandidate]:
    """Single-name (> 20%) and sector (> 50%) concentration (docs/05). Deterministic
    order: names sorted, then sectors sorted."""
    out: list[FlagCandidate] = []
    for symbol in sorted(name_weights):
        weight = name_weights[symbol]
        if weight > _NAME_WEIGHT_MAX:
            out.append(FlagCandidate("concentration", "warn", symbol, None, float(weight),
                                     "single_name_concentration"))
    for sector_id in sorted(sector_weights):
        weight = sector_weights[sector_id]
        if weight > _SECTOR_WEIGHT_MAX:
            out.append(FlagCandidate("concentration", "warn", None, sector_id, float(weight),
                                     "sector_concentration"))
    return out


def correlation_flag(mean_corr: float | None) -> FlagCandidate | None:
    if mean_corr is None or mean_corr <= _CORR_MAX:
        return None
    return FlagCandidate("correlation", "warn", None, None, mean_corr, "corr_20d_high")


def candidate_dict(c: FlagCandidate) -> dict[str, object]:
    """The flag shape the BriefObject carries (contract ``flag`` def)."""
    return {
        "type": c.type,
        "severity": c.severity,
        "symbol": c.symbol,
        "sector_id": c.sector_id,
        "value": c.value,
        "text_key": c.text_key,
    }


# --- Database layer -------------------------------------------------------

_READ_SECTORS = text("""
    SELECT symbol, sector_id FROM holdings WHERE user_id = :user_id
""")

_READ_FUNDAMENTALS = text("""
    SELECT symbol, as_of, cash_cents, quarterly_burn_cents, shares_out, next_earnings_date
    FROM fundamentals
    WHERE symbol = ANY(:symbols) AND as_of <= :session_date
    ORDER BY symbol, as_of
""")

_READ_NEXT_SUPPLY = text("""
    SELECT symbol, min(occurs_at) AS occurs_at FROM events
    WHERE symbol = ANY(:symbols) AND event_type = 'lockup' AND occurs_at >= :session_date
    GROUP BY symbol
""")

_READ_RETURNS = text("""
    SELECT symbol, c FROM (
        SELECT symbol, session_date, c,
               row_number() OVER (PARTITION BY symbol ORDER BY session_date DESC) AS rn
        FROM bars_daily
        WHERE symbol = ANY(:symbols) AND session_date <= :session_date
    ) ranked
    WHERE rn <= :depth
    ORDER BY symbol, session_date
""")

_READ_FLAG_STATE = text("""
    SELECT flag_type, symbol, sector_id, last_seen FROM flags WHERE user_id = :user_id
""")

_UPSERT_FLAG = text("""
    INSERT INTO flags (user_id, flag_type, symbol, sector_id, first_seen, last_seen,
                       severity, payload)
    VALUES (:user_id, :flag_type, :symbol, :sector_id, :session_date, :session_date,
            :severity, CAST(:payload AS jsonb))
    ON CONFLICT (user_id, flag_type, symbol, sector_id) DO UPDATE
        SET last_seen = EXCLUDED.last_seen, severity = EXCLUDED.severity
""")


def surface_flags(
    conn: Connection, user_id: str, session_date: date, result: ComputeResult
) -> list[FlagCandidate]:
    """Build every fired flag candidate, then drop the weekly-capped ones that
    were already mentioned inside the last ``_RATE_LIMIT_DAYS`` days. Read-only —
    ``record_flags`` writes ``last_seen`` after the brief is confirmed to send, so
    a skipped quiet session spends no rate-limit budget."""
    candidates = _build_candidates(conn, user_id, session_date, result)
    last_seen = _read_flag_state(conn, user_id)

    surfaced: list[FlagCandidate] = []
    for c in candidates:
        if c.type in _WEEKLY_CAPPED:
            prior = last_seen.get((c.type, c.symbol, c.sector_id))
            if prior is not None and (session_date - prior).days < _RATE_LIMIT_DAYS:
                continue  # mentioned this week already — stays on the dashboard, off the email
        surfaced.append(c)
    return surfaced


def record_flags(
    conn: Connection, user_id: str, session_date: date, surfaced: list[FlagCandidate]
) -> None:
    """Persist that these flags were surfaced today — bumping ``last_seen`` (the
    rate-limit clock) and setting ``first_seen`` on first sighting."""
    for c in surfaced:
        conn.execute(
            _UPSERT_FLAG,
            {
                "user_id": user_id,
                "flag_type": c.type,
                "symbol": c.symbol,
                "sector_id": c.sector_id,
                "session_date": session_date,
                "severity": c.severity,
                "payload": "{}",
            },
        )


def _build_candidates(
    conn: Connection, user_id: str, session_date: date, result: ComputeResult
) -> list[FlagCandidate]:
    symbols = [p.symbol for p in result.positions]
    out: list[FlagCandidate] = []

    # Concentration (name + sector) from the weights compute already produced.
    sector_by_symbol = _read_sectors(conn, user_id)
    name_weights = {p.symbol: p.weight for p in result.positions if p.weight is not None}
    sector_weights: dict[str, Fraction] = {}
    for symbol, weight in name_weights.items():
        sector = sector_by_symbol.get(symbol)
        if sector is not None:
            sector_weights[sector] = sector_weights.get(sector, Fraction(0)) + weight
    out.extend(concentration_flags(name_weights, sector_weights))

    # Correlation over trailing daily returns.
    corr = mean_pairwise_corr(_read_returns(conn, symbols, session_date))
    corr_flag = correlation_flag(corr)
    if corr_flag is not None:
        out.append(corr_flag)

    # Position risk, per name, from fundamentals + events.
    fundamentals = _read_fundamentals(conn, symbols, session_date)
    supply = _read_next_supply(conn, symbols, session_date)
    for symbol in symbols:
        rows = fundamentals.get(symbol, [])
        out.extend(_position_risk(symbol, rows, supply.get(symbol), session_date))
    return out


def _position_risk(
    symbol: str, rows: list[_Fundamental], next_supply: date | None, session_date: date
) -> list[FlagCandidate]:
    maybe: list[FlagCandidate | None] = []
    if rows:
        latest = rows[-1]
        burns = [r.quarterly_burn_cents for r in rows if r.quarterly_burn_cents is not None]
        if latest.cash_cents is not None:
            maybe.append(runway_flag(symbol, runway_quarters(latest.cash_cents, burns[-4:])))
        maybe.append(dilution_flag(symbol, _dilution_from(rows)))
        if latest.next_earnings_date is not None:
            days = (latest.next_earnings_date - session_date).days
            maybe.append(earnings_soon_flag(symbol, days))
    if next_supply is not None:
        maybe.append(supply_event_flag(symbol, (next_supply - session_date).days))
    return [c for c in maybe if c is not None]


def _dilution_from(rows: list[_Fundamental]) -> Fraction | None:
    """Latest shares vs the most recent reading at least a year older."""
    latest = rows[-1]
    if latest.shares_out is None:
        return None
    cutoff = date(latest.as_of.year - 1, latest.as_of.month, latest.as_of.day)
    base = next((r for r in reversed(rows) if r.as_of <= cutoff), None)
    if base is None or base.shares_out is None:
        return None
    return dilution_yoy(latest.shares_out, base.shares_out)


def _read_sectors(conn: Connection, user_id: str) -> dict[str, str]:
    return {
        row["symbol"]: str(row["sector_id"])
        for row in conn.execute(_READ_SECTORS, {"user_id": user_id}).mappings()
    }


_FlagKey = tuple[str, str | None, str | None]


def _read_flag_state(conn: Connection, user_id: str) -> dict[_FlagKey, date]:
    """Each flag's last-surfaced date, keyed by (type, symbol, sector_id) — the
    rate-limit clock."""
    return {
        (row["flag_type"], row["symbol"], _opt_str(row["sector_id"])): row["last_seen"]
        for row in conn.execute(_READ_FLAG_STATE, {"user_id": user_id}).mappings()
    }


def _read_fundamentals(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, list[_Fundamental]]:
    if not symbols:
        return {}
    out: dict[str, list[_Fundamental]] = {}
    for row in conn.execute(
        _READ_FUNDAMENTALS, {"symbols": symbols, "session_date": session_date}
    ).mappings():
        out.setdefault(row["symbol"], []).append(
            _Fundamental(
                as_of=row["as_of"],
                cash_cents=_opt_int(row["cash_cents"]),
                quarterly_burn_cents=_opt_int(row["quarterly_burn_cents"]),
                shares_out=_opt_int(row["shares_out"]),
                next_earnings_date=row["next_earnings_date"],
            )
        )
    return out


def _opt_int(value: object) -> int | None:
    return int(value) if value is not None else None  # type: ignore[call-overload]


def _opt_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _read_next_supply(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, date]:
    if not symbols:
        return {}
    return {
        row["symbol"]: row["occurs_at"]
        for row in conn.execute(
            _READ_NEXT_SUPPLY, {"symbols": symbols, "session_date": session_date}
        ).mappings()
    }


def _read_returns(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, list[float]]:
    """Trailing daily returns per symbol (newest window of ``_CORR_WINDOW``),
    aligned to the shortest common length so pairwise correlation lines up."""
    if not symbols:
        return {}
    closes: dict[str, list[float]] = {}
    for row in conn.execute(
        _READ_RETURNS, {"symbols": symbols, "session_date": session_date, "depth": _CORR_WINDOW + 1}
    ).mappings():
        closes.setdefault(row["symbol"], []).append(float(row["c"]))

    returns: dict[str, list[float]] = {}
    for symbol, series in closes.items():
        rets = [
            (series[i] - series[i - 1]) / series[i - 1]
            for i in range(1, len(series))
            if series[i - 1] != 0
        ]
        if len(rets) >= _CORR_WINDOW:
            returns[symbol] = rets[-_CORR_WINDOW:]
    return returns
