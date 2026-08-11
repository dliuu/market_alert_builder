"""Stage ③ compute: returns, P&L, and contribution over bars_daily + lots.

``compute`` is a pure function of its inputs — no network, clock, or float — so
every figure is unit-testable and a re-run reproduces identical metrics. Money
is integer cents (rounded per position, the way a broker rounds); ratios are
exact ``Fraction``s so that ``Σ contribution_bps == book day_bps`` holds
mathematically, not within a tolerance (invariant 3). Fractions are converted to
Decimal only at storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.constants import BENCHMARK_SYMBOL

_CENTS = Decimal("100")
_BPS = 10_000


# --- Inputs ---------------------------------------------------------------


@dataclass(frozen=True)
class Lot:
    symbol: str
    shares: Decimal
    cost_basis_cents: int  # per-share cost, in cents
    opened_on: date


@dataclass(frozen=True)
class Price:
    c: Decimal  # session close, dollars
    prev_c: Decimal | None  # previous session close, dollars (None on first bar)


# --- Outputs --------------------------------------------------------------


@dataclass(frozen=True)
class PositionMetrics:
    symbol: str
    day_return: Fraction | None
    value_cents: int
    prior_value_cents: int
    day_pnl_cents: int
    total_pnl_cents: int
    total_cost_cents: int
    contribution_bps: Fraction | None
    weight: Fraction | None


@dataclass(frozen=True)
class BookMetrics:
    value_cents: int
    prior_value_cents: int
    day_pnl_cents: int
    total_pnl_cents: int
    total_cost_cents: int
    day_bps: Fraction | None
    total_pct: Fraction | None
    vs_spy_bps: Fraction | None


@dataclass(frozen=True)
class ComputeResult:
    positions: list[PositionMetrics]
    book: BookMetrics


@dataclass
class _Aggregate:
    """Per-position cents, summed before the book-level ratios are known."""

    symbol: str
    value_cents: int
    prior_value_cents: int
    day_pnl_cents: int
    total_pnl_cents: int
    total_cost_cents: int
    day_return: Fraction | None


def compute(
    session_date: date,
    lots: list[Lot],
    prices: dict[str, Price],
    *,
    benchmark_return: Fraction | None = None,
) -> ComputeResult:
    """Compute per-position and book metrics for one session. ``prices`` must
    contain every held symbol; a held lot whose symbol lacks a prior close is a
    data gap and raises (graceful degradation is deferred to M5)."""
    by_symbol: dict[str, list[Lot]] = {}
    for lot in lots:
        by_symbol.setdefault(lot.symbol, []).append(lot)

    aggregates = [
        _aggregate(symbol, by_symbol[symbol], prices[symbol], session_date)
        for symbol in sorted(by_symbol)
    ]

    book_value = sum(a.value_cents for a in aggregates)
    book_prior = sum(a.prior_value_cents for a in aggregates)
    book_day_pnl = sum(a.day_pnl_cents for a in aggregates)
    book_total_pnl = sum(a.total_pnl_cents for a in aggregates)
    book_total_cost = sum(a.total_cost_cents for a in aggregates)

    positions = [
        PositionMetrics(
            symbol=a.symbol,
            day_return=a.day_return,
            value_cents=a.value_cents,
            prior_value_cents=a.prior_value_cents,
            day_pnl_cents=a.day_pnl_cents,
            total_pnl_cents=a.total_pnl_cents,
            total_cost_cents=a.total_cost_cents,
            # Same denominator (book_prior) as day_bps → the parts sum to the whole.
            contribution_bps=_bps(a.day_pnl_cents, book_prior),
            weight=_ratio(a.value_cents, book_value),
        )
        for a in aggregates
    ]

    book = BookMetrics(
        value_cents=book_value,
        prior_value_cents=book_prior,
        day_pnl_cents=book_day_pnl,
        total_pnl_cents=book_total_pnl,
        total_cost_cents=book_total_cost,
        day_bps=_bps(book_day_pnl, book_prior),
        total_pct=_ratio(book_total_pnl, book_total_cost),
        vs_spy_bps=_vs_spy(book_day_pnl, book_prior, benchmark_return),
    )
    return ComputeResult(positions=positions, book=book)


def _aggregate(symbol: str, lots: list[Lot], price: Price, session_date: date) -> _Aggregate:
    close_cents = price.c * _CENTS
    value = Decimal(0)
    prior_value = Decimal(0)
    day_pnl = Decimal(0)
    total_pnl = Decimal(0)
    total_cost = Decimal(0)

    for lot in lots:
        basis_cents = Decimal(lot.cost_basis_cents)
        value += lot.shares * close_cents
        total_cost += lot.shares * basis_cents
        total_pnl += lot.shares * (close_cents - basis_cents)
        if lot.opened_on < session_date:
            if price.prev_c is None:
                raise ValueError(f"missing prev_close for held symbol {symbol} on {session_date}")
            prev_cents = price.prev_c * _CENTS
            prior_value += lot.shares * prev_cents
            day_pnl += lot.shares * (close_cents - prev_cents)
        else:
            # Opened today: the day's P&L is measured from the purchase price, and
            # it held no value at the prior close.
            day_pnl += lot.shares * (close_cents - basis_cents)

    return _Aggregate(
        symbol=symbol,
        value_cents=_round(value),
        prior_value_cents=_round(prior_value),
        day_pnl_cents=_round(day_pnl),
        total_pnl_cents=_round(total_pnl),
        total_cost_cents=_round(total_cost),
        day_return=_day_return(price),
    )


def _day_return(price: Price) -> Fraction | None:
    if price.prev_c is None or price.prev_c == 0:
        return None
    return (Fraction(price.c) - Fraction(price.prev_c)) / Fraction(price.prev_c)


def _vs_spy(
    book_day_pnl: int, book_prior: int, benchmark_return: Fraction | None
) -> Fraction | None:
    day_bps = _bps(book_day_pnl, book_prior)
    if day_bps is None or benchmark_return is None:
        return None
    return day_bps - benchmark_return * _BPS


def _bps(numerator_cents: int, denominator_cents: int) -> Fraction | None:
    if denominator_cents <= 0:
        return None
    return Fraction(numerator_cents, denominator_cents) * _BPS


def _ratio(numerator_cents: int, denominator_cents: int) -> Fraction | None:
    if denominator_cents <= 0:
        return None
    return Fraction(numerator_cents, denominator_cents)


def _round(cents: Decimal) -> int:
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --- Database layer -------------------------------------------------------

# Fractions are stored as Decimal at this scale; plenty for bps and fractions.
_STORE_SCALE = Decimal("0.0000000001")

_READ_LOTS = text("""
    SELECT h.symbol AS symbol, l.shares AS shares,
           l.cost_basis_cents AS cost_basis_cents, l.opened_on AS opened_on
    FROM lots l
    JOIN holdings h ON h.id = l.holding_id
    WHERE l.user_id = :user_id
      AND l.opened_on <= :session_date
      AND (l.closed_on IS NULL OR l.closed_on > :session_date)
""")

_READ_PRICES = text("""
    SELECT symbol, c, prev_c FROM (
        SELECT symbol, session_date, c,
               LAG(c) OVER (PARTITION BY symbol ORDER BY session_date) AS prev_c
        FROM bars_daily
    ) ranked
    WHERE session_date = :session_date
""")

_UPSERT = text("""
    INSERT INTO metrics (user_id, symbol, session_date, metric, value)
    VALUES (:user_id, :symbol, :session_date, :metric, :value)
    ON CONFLICT (user_id, symbol, session_date, metric) DO UPDATE SET value = EXCLUDED.value
""")


def compute_and_store(conn: Connection, user_id: str, session_date: date) -> ComputeResult:
    """Read the book and bars, compute, and persist per-symbol metrics. Reads
    are for the given user; the benchmark (SPY) feeds vs_spy but isn't stored."""
    lots = _read_lots(conn, user_id, session_date)
    needed = sorted({lot.symbol for lot in lots} | {BENCHMARK_SYMBOL})
    prices = _read_prices(conn, needed, session_date)

    missing = sorted({lot.symbol for lot in lots} - prices.keys())
    if missing:
        raise ValueError(
            f"no bars for {', '.join(missing)} on {session_date}; run backfill for these symbols"
        )

    benchmark = prices.get(BENCHMARK_SYMBOL)
    benchmark_return = _day_return(benchmark) if benchmark else None

    result = compute(session_date, lots, prices, benchmark_return=benchmark_return)
    _store(conn, user_id, session_date, result)
    return result


def _read_lots(conn: Connection, user_id: str, session_date: date) -> list[Lot]:
    rows = conn.execute(
        _READ_LOTS, {"user_id": user_id, "session_date": session_date}
    ).mappings().all()
    return [
        Lot(
            symbol=row["symbol"],
            shares=Decimal(str(row["shares"])),
            cost_basis_cents=int(row["cost_basis_cents"]),
            opened_on=row["opened_on"],
        )
        for row in rows
    ]


def _read_prices(conn: Connection, symbols: list[str], session_date: date) -> dict[str, Price]:
    rows = conn.execute(_READ_PRICES, {"session_date": session_date}).mappings().all()
    wanted = set(symbols)
    prices: dict[str, Price] = {}
    for row in rows:
        if row["symbol"] not in wanted:
            continue
        prev = row["prev_c"]
        prices[row["symbol"]] = Price(
            c=Decimal(str(row["c"])),
            prev_c=Decimal(str(prev)) if prev is not None else None,
        )
    return prices


def _store(conn: Connection, user_id: str, session_date: date, result: ComputeResult) -> None:
    for position in result.positions:
        values: dict[str, Decimal | None] = {
            "day_return": _to_decimal(position.day_return),
            "day_pnl_cents": Decimal(position.day_pnl_cents),
            "total_pnl_cents": Decimal(position.total_pnl_cents),
            "contribution_bps": _to_decimal(position.contribution_bps),
            "weight": _to_decimal(position.weight),
        }
        for metric, value in values.items():
            if value is None:
                continue
            conn.execute(
                _UPSERT,
                {
                    "user_id": user_id,
                    "symbol": position.symbol,
                    "session_date": session_date,
                    "metric": metric,
                    "value": value,
                },
            )


def _to_decimal(value: Fraction | None) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        _STORE_SCALE, rounding=ROUND_HALF_UP
    )
