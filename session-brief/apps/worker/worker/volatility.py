"""Option premium and volatility (M22): the strip, IV30 and realized vol.

Three jobs. ``select_strip`` reads a chain into twelve call cells (four tenors
by three strike rules). ``iv30_from_chain`` builds a constant-maturity 30-day
implied vol from the same chain, and ``realized_vol`` its backward-looking
counterpart from closes. Percentile standing of the result reuses the M20 rule
in ``worker/oscillators.py``: ranked against the symbol's own trailing year.

The pure half comes first and touches no database; ``Decimal`` throughout,
under the same explicit 28-digit context as ``oscillators``. The database layer
(``compute_and_store_volatility``) rebuilds each chain from ``option_snapshot``
and writes the reading to ``metrics`` and ``option_strip``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from statistics import median

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.calendar import previous_session
from worker.constants import (
    EARNINGS_WINDOW_MIN_DAYS,
    HV_WINDOWS,
    IV30_DRIFT_ALARM_POINTS,
    IV30_MIN_CONTRACTS,
    IV30_NEAR_MIN_DTE,
    IV30_PARITY_TOL_FRACTION,
    IV30_PARITY_TOL_POINTS,
    IV30_STRIKES_EACH_SIDE,
    IV30_TARGET_DAYS,
    MIN_DTE,
    PREMIUM_CHEAP_PCTILE,
    PREMIUM_RICH_PCTILE,
    STRIKE_RULES,
    TARGET_MISS_TOL,
    TENORS,
)
from worker.options import Contract
from worker.oscillators import BASELINE_SESSIONS, percentile

PREC = 28
_TRADING_DAYS = Decimal(252)
_HUNDRED = Decimal(100)

# --- pure --------------------------------------------------------------------


@dataclass(frozen=True)
class StripCell:
    tenor: int
    rule: str
    target: Decimal  # multiple of spot the rule asked for
    contract: Contract | None
    dte: int | None
    moneyness: Decimal | None  # strike / spot - 1
    mid: Decimal | None
    premium_pct: Decimal | None  # mid / spot
    time_value_pct: Decimal | None  # (mid - intrinsic) / spot
    target_miss: bool
    tenor_collapsed: bool


def select_strip(contracts: list[Contract], spot: Decimal, session_date: date) -> list[StripCell]:
    """Twelve cells per name: for each tenor the listed expiry nearest it (tie
    → nearer), and at that expiry the two-sided call nearest each target
    strike (tie → lower). A tenor that resolves to an expiry an earlier tenor
    already took is marked ``tenor_collapsed`` and still reported."""
    calls = [
        c
        for c in contracts
        if c.right == "C" and c.two_sided and (c.expiry - session_date).days >= MIN_DTE
    ]
    expiries = sorted({c.expiry for c in calls})
    used: set[date] = set()
    cells: list[StripCell] = []
    for tenor in TENORS:
        if not expiries:
            cells.extend(
                StripCell(tenor, rule, target, None, None, None, None, None, None, False, False)
                for rule, target in STRIKE_RULES
            )
            continue
        expiry = min(
            expiries,
            key=lambda e: (abs((e - session_date).days - tenor), (e - session_date).days),
        )
        collapsed = expiry in used
        used.add(expiry)
        dte = (expiry - session_date).days
        at_expiry = [c for c in calls if c.expiry == expiry]
        for rule, target in STRIKE_RULES:
            want = spot * target
            chosen = min(at_expiry, key=lambda c: (abs(c.strike - want), c.strike))
            moneyness = chosen.strike / spot - 1
            intrinsic = max(spot - chosen.strike, Decimal(0))
            cells.append(
                StripCell(
                    tenor=tenor,
                    rule=rule,
                    target=target,
                    contract=chosen,
                    dte=dte,
                    moneyness=moneyness,
                    mid=chosen.mid,
                    premium_pct=chosen.mid / spot,
                    time_value_pct=(chosen.mid - intrinsic) / spot,
                    target_miss=abs(moneyness - (target - 1)) > TARGET_MISS_TOL,
                    tenor_collapsed=collapsed,
                )
            )
    return cells


@dataclass(frozen=True)
class ExpiryIv:
    expiry: date
    dte: int
    iv: Decimal  # vol points, mean over the contributing contracts
    n: int


@dataclass(frozen=True)
class Iv30:
    iv30: Decimal | None  # vol points
    n: int
    near: ExpiryIv | None
    far: ExpiryIv | None
    dropped: tuple[str, ...]  # "YYYY-MM-DD strike" pairs the parity check removed
    contributors: tuple[str, ...]  # symbols of the contracts whose iv fed the number


def iv30_from_chain(contracts: list[Contract], spot: Decimal, session_date: date) -> Iv30:
    eligible = [
        c
        for c in contracts
        if c.two_sided and c.iv is not None and (c.expiry - session_date).days >= IV30_NEAR_MIN_DTE
    ]
    expiries = sorted({c.expiry for c in eligible})
    if not expiries:
        return Iv30(None, 0, None, None, (), ())

    def dte(e: date) -> int:
        return (e - session_date).days

    at_or_below = [e for e in expiries if dte(e) <= IV30_TARGET_DAYS]
    above = [e for e in expiries if dte(e) > IV30_TARGET_DAYS]
    near_expiry = max(at_or_below) if at_or_below else None
    far_expiry = min(above) if above else None
    dropped: list[str] = []
    contributors: list[str] = []

    def expiry_iv(e: date) -> ExpiryIv | None:
        at_expiry = [c for c in eligible if c.expiry == e]
        strikes = sorted({c.strike for c in at_expiry})
        below = [k for k in strikes if k <= spot][-IV30_STRIKES_EACH_SIDE:]
        above_spot = [k for k in strikes if k > spot][:IV30_STRIKES_EACH_SIDE]
        ivs: list[Decimal] = []
        for k in below + above_spot:
            call = next((c for c in at_expiry if c.strike == k and c.right == "C"), None)
            put = next((c for c in at_expiry if c.strike == k and c.right == "P"), None)
            if call is not None and put is not None:
                assert call.iv is not None and put.iv is not None  # eligible ⇒ iv present
                tolerance = max(
                    IV30_PARITY_TOL_POINTS, IV30_PARITY_TOL_FRACTION * (call.iv + put.iv) / 2
                )
                if abs(call.iv - put.iv) > tolerance:
                    dropped.append(f"{e.isoformat()} {k}")
                    continue
            for c in (call, put):
                if c is not None and c.iv is not None:
                    ivs.append(c.iv)
                    contributors.append(c.symbol)
        if not ivs:
            return None
        return ExpiryIv(expiry=e, dte=dte(e), iv=sum(ivs, Decimal(0)) / len(ivs), n=len(ivs))

    near = expiry_iv(near_expiry) if near_expiry is not None else None
    far = expiry_iv(far_expiry) if far_expiry is not None else None
    iv30: Decimal | None
    if near is not None and far is not None and near.dte != IV30_TARGET_DAYS:
        # Linear in total variance (iv² · T), the standard constant-maturity
        # construction; a straight line in iv would understate the near leg.
        var_near = near.iv * near.iv * near.dte
        var_far = far.iv * far.iv * far.dte
        weight = Decimal(IV30_TARGET_DAYS - near.dte) / Decimal(far.dte - near.dte)
        var_30 = var_near + (var_far - var_near) * weight
        iv30 = (var_30 / IV30_TARGET_DAYS).sqrt()
    elif near is not None:
        iv30 = near.iv
    elif far is not None:
        iv30 = far.iv
    else:
        iv30 = None
    n = (near.n if near else 0) + (far.n if far else 0)
    return Iv30(
        iv30=iv30,
        n=n,
        near=near,
        far=far,
        dropped=tuple(dropped),
        contributors=tuple(contributors),
    )


def selected_contracts(
    contracts: list[Contract], spot: Decimal, session_date: date
) -> list[Contract]:
    """Every contract the strip or IV30 consulted, deduped, ordered by symbol."""
    wanted = set(iv30_from_chain(contracts, spot, session_date).contributors)
    wanted |= {
        cell.contract.symbol
        for cell in select_strip(contracts, spot, session_date)
        if cell.contract is not None
    }
    by_symbol = {c.symbol: c for c in contracts if c.symbol in wanted}
    return [by_symbol[s] for s in sorted(by_symbol)]


def realized_vol(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """Annualised sample stdev of the ``window`` daily log returns ending at the
    prior session, in vol points. ``closes`` run oldest → newest with today
    last; today's close is excluded (it may be an intraday print), so fewer
    than ``window + 2`` closes yields ``None``."""
    if len(closes) < window + 2:
        return None
    used = closes[-(window + 2) : -1]
    with localcontext() as ctx:
        ctx.prec = PREC
        returns = [(b / a).ln() for a, b in zip(used, used[1:], strict=False)]
        mean = sum(returns, Decimal(0)) / window
        variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (window - 1)
        return variance.sqrt() * _TRADING_DAYS.sqrt() * _HUNDRED


def term_slope(cells: list[StripCell]) -> Decimal | None:
    """7-day ATM IV minus 30-day ATM IV; ``None`` when either is missing or the
    two tenors resolved to the same expiry."""
    by_key = {(c.tenor, c.rule): c for c in cells}
    short, long = by_key.get((7, "atm")), by_key.get((30, "atm"))
    if short is None or long is None or short.contract is None or long.contract is None:
        return None
    if short.dte == long.dte:
        return None
    if short.contract.iv is None or long.contract.iv is None:
        return None
    return short.contract.iv - long.contract.iv


@dataclass(frozen=True)
class StripRow:
    """One stored strip cell: the ``StripCell`` measurement plus its quote
    fields and three changes against the prior session, each ``None`` when its
    prior is missing."""

    tenor: int
    rule: str
    contract: str | None
    dte: int | None
    moneyness: Decimal | None
    mid: Decimal | None
    premium_pct: Decimal | None
    time_value_pct: Decimal | None
    iv: Decimal | None
    delta: Decimal | None
    open_interest: int | None
    volume: int | None
    chg_contract_pct: Decimal | None  # same contract's mid vs our prior snapshot
    chg_tenor_pct: Decimal | None  # same tenor-and-rule cell's premium_pct
    iv_chg: Decimal | None  # same cell's iv, vol points
    tenor_collapsed: bool
    target_miss: bool


def _change(now: Decimal | None, then: Decimal | None) -> Decimal | None:
    if now is None or then is None or then == 0:
        return None
    return now / then - 1


def strip_rows(
    cells: list[StripCell],
    prior: dict[tuple[int, str], StripRow],
    prior_mids: dict[str, Decimal],
) -> list[StripRow]:
    """``cells`` as rows, with the changes against ``prior`` (the previous
    session's rows keyed by tenor and rule) and ``prior_mids`` (the previous
    session's mid per contract)."""
    rows: list[StripRow] = []
    for cell in cells:
        c = cell.contract
        before = prior.get((cell.tenor, cell.rule))
        iv = c.iv if c is not None else None
        iv_chg = (
            iv - before.iv
            if iv is not None and before is not None and before.iv is not None
            else None
        )
        rows.append(
            StripRow(
                tenor=cell.tenor,
                rule=cell.rule,
                contract=c.symbol if c is not None else None,
                dte=cell.dte,
                moneyness=cell.moneyness,
                mid=cell.mid,
                premium_pct=cell.premium_pct,
                time_value_pct=cell.time_value_pct,
                iv=iv,
                delta=c.delta if c is not None else None,
                open_interest=c.open_interest if c is not None else None,
                volume=c.volume if c is not None else None,
                chg_contract_pct=_change(
                    cell.mid, prior_mids.get(c.symbol) if c is not None else None
                ),
                chg_tenor_pct=_change(
                    cell.premium_pct, before.premium_pct if before is not None else None
                ),
                iv_chg=iv_chg,
                tenor_collapsed=cell.tenor_collapsed,
                target_miss=cell.target_miss,
            )
        )
    return rows


@dataclass(frozen=True)
class VolReading:
    """One symbol's premium reading for a session. Every level carries its
    comparison base — its own year, realized vol, or the tape — and a level
    whose baseline is short keeps the value and nulls the comparison, the M20
    rule. ``premium`` is recomputed by ``regime`` wherever it is read, never
    stored; ``stale`` marks a chain carried from the prior session."""

    symbol: str
    close: Decimal
    iv30: Decimal | None
    iv30_n: int
    iv30_cboe: Decimal | None
    iv30_thin: bool
    iv30_pctile: Decimal | None
    iv_rank_252: Decimal | None
    hv20: Decimal | None
    hv60: Decimal | None
    iv_hv_spread: Decimal | None
    iv_hv_ratio: Decimal | None
    iv_vix_ratio: Decimal | None
    iv_vix_ratio_pctile: Decimal | None
    vix_close: Decimal | None
    vix_pctile: Decimal | None
    iv_term_slope: Decimal | None
    premium: str  # "rich" | "fair" | "cheap"
    earnings_date: date | None
    contracts_writable: int
    stale: bool
    strip: tuple[StripRow, ...]


def regime(
    *,
    iv30_pctile: Decimal | None,
    iv_hv_spread: Decimal | None,
    iv30_thin: bool,
    iv_vix_ratio: Decimal | None,
    book_median_ratio: Decimal | None,
) -> str:
    """``rich`` when implied runs over realized and the name's own year (or,
    with no full year yet, the book's median IV/VIX ratio) puts it high;
    ``cheap`` when implied runs under realized or the year puts it low;
    ``fair`` otherwise. A thin IV30, or no realized comparison, is ``fair``."""
    if iv30_thin or iv_hv_spread is None:
        return "fair"
    if iv_hv_spread > 0:
        if iv30_pctile is not None:
            if iv30_pctile >= PREMIUM_RICH_PCTILE:
                return "rich"
        elif (
            iv_vix_ratio is not None
            and book_median_ratio is not None
            and iv_vix_ratio >= book_median_ratio
        ):
            return "rich"
    if iv_hv_spread < 0 or (iv30_pctile is not None and iv30_pctile <= PREMIUM_CHEAP_PCTILE):
        return "cheap"
    return "fair"


def regime_from_metrics(
    values: dict[str, Decimal], book_median_ratio: Decimal | None, *, iv30_thin: bool
) -> str:
    """``regime`` over one name's stored ``metrics`` for a session — the same
    rule, recomputed from the numbers because the enum is never stored."""
    return regime(
        iv30_pctile=values.get("iv30_pctile"),
        iv_hv_spread=values.get("iv_hv_spread"),
        iv30_thin=iv30_thin,
        iv_vix_ratio=values.get("iv_vix_ratio"),
        book_median_ratio=book_median_ratio,
    )


# --- database layer ----------------------------------------------------------

_STORE_SCALE = Decimal("0.0000000001")
_BAR_DEPTH = max(HV_WINDOWS) + 2  # realized_vol's window plus the excluded today
_VIX_DEPTH = BASELINE_SESSIONS + 1

_READ_CHAINS = text("""
    SELECT c.symbol AS underlying, s.contract, c.expiration_date, c.put_or_call, c.strike,
           s.bid, s.ask, s.bid_size, s.ask_size, s.iv, s.delta, s.gamma, s.theta, s.vega,
           s.rho, s.open_interest, s.volume, s.last, s.prev_day_close
    FROM option_snapshot s
    JOIN option_contracts c USING (contract)
    WHERE c.symbol = ANY(:symbols) AND s.session_date = :session_date
    ORDER BY s.contract
""")

_ANY_SNAPSHOT = text("""
    SELECT EXISTS (
        SELECT 1 FROM option_snapshot s
        JOIN option_contracts c USING (contract)
        WHERE c.symbol = ANY(:symbols) AND s.session_date <= :session_date
    )
""")

_READ_BARS = text("""
    SELECT symbol, session_date, c, adj_c FROM (
        SELECT symbol, session_date, c, adj_c,
               row_number() OVER (
                   PARTITION BY symbol ORDER BY session_date DESC
               ) AS rn
        FROM bars_daily
        WHERE symbol = ANY(:symbols) AND session_date <= :session_date
    ) ranked
    WHERE rn <= :depth
    ORDER BY symbol, session_date
""")

# Each metric's own trailing year, today excluded: the percentile baseline.
_READ_HISTORY = text("""
    SELECT symbol, metric, value FROM (
        SELECT symbol, metric, session_date, value,
               row_number() OVER (
                   PARTITION BY symbol, metric ORDER BY session_date DESC
               ) AS rn
        FROM metrics
        WHERE user_id = :user_id AND symbol = ANY(:symbols)
          AND metric = ANY(:metrics) AND session_date < :session_date
    ) ranked
    WHERE rn <= :depth
    ORDER BY symbol, metric, session_date
""")

_READ_VIX = text("""
    SELECT session_date, c FROM (
        SELECT session_date, c FROM index_daily
        WHERE symbol = 'VIX' AND session_date <= :session_date
        ORDER BY session_date DESC
        LIMIT :depth
    ) recent
    ORDER BY session_date
""")

_READ_EARNINGS = text("""
    SELECT min(occurs_at) FROM events
    WHERE symbol = :symbol AND event_type = 'earnings'
      AND occurs_at >= :session_date AND occurs_at <= :horizon
""")

_READ_SHARES = text("""
    SELECT h.symbol AS symbol, sum(l.shares) AS shares
    FROM lots l
    JOIN holdings h ON h.id = l.holding_id
    WHERE l.user_id = :user_id AND h.symbol = ANY(:symbols)
      AND l.opened_on <= :session_date
      AND (l.closed_on IS NULL OR l.closed_on > :session_date)
    GROUP BY h.symbol
""")

_READ_PRIOR_STRIP = text("""
    SELECT symbol, tenor, strike_rule, premium_pct, iv FROM option_strip
    WHERE symbol = ANY(:symbols) AND session_date = :session_date
""")

_READ_UNDERLYING = text("""
    SELECT body->'data'->>'iv30' AS iv30, body->'data'->>'close' AS close FROM raw_payloads
    WHERE source = 'cboe' AND endpoint = 'delayed_quotes/options'
      AND symbol = :symbol AND as_of = :as_of
""")

_UPSERT_METRIC = text("""
    INSERT INTO metrics (user_id, symbol, session_date, metric, value)
    VALUES (:user_id, :symbol, :session_date, :metric, :value)
    ON CONFLICT (user_id, symbol, session_date, metric)
    DO UPDATE SET value = EXCLUDED.value
""")

_UPSERT_STRIP = text("""
    INSERT INTO option_strip
        (symbol, session_date, tenor, strike_rule, contract, dte, moneyness, mid,
         premium_pct, time_value_pct, iv, delta, open_interest, volume,
         chg_contract_pct, chg_tenor_pct, iv_chg, tenor_collapsed, target_miss)
    VALUES (:symbol, :session_date, :tenor, :strike_rule, :contract, :dte, :moneyness, :mid,
            :premium_pct, :time_value_pct, :iv, :delta, :open_interest, :volume,
            :chg_contract_pct, :chg_tenor_pct, :iv_chg, :tenor_collapsed, :target_miss)
    ON CONFLICT (symbol, session_date, tenor, strike_rule) DO UPDATE SET
        contract = EXCLUDED.contract, dte = EXCLUDED.dte, moneyness = EXCLUDED.moneyness,
        mid = EXCLUDED.mid, premium_pct = EXCLUDED.premium_pct,
        time_value_pct = EXCLUDED.time_value_pct, iv = EXCLUDED.iv,
        delta = EXCLUDED.delta, open_interest = EXCLUDED.open_interest,
        volume = EXCLUDED.volume, chg_contract_pct = EXCLUDED.chg_contract_pct,
        chg_tenor_pct = EXCLUDED.chg_tenor_pct, iv_chg = EXCLUDED.iv_chg,
        tenor_collapsed = EXCLUDED.tenor_collapsed, target_miss = EXCLUDED.target_miss
""")


def compute_and_store_volatility(
    conn: Connection,
    user_id: str,
    symbols: list[str],
    session_date: date,
) -> dict[str, VolReading]:
    """The premium reading for ``symbols`` on ``session_date``, persisted to
    ``metrics`` and ``option_strip`` and returned keyed by symbol.

    The chain is measured against the close in its stored payload, the one
    ingest selected contracts against, and against ``bars_daily.c`` only when
    that payload is absent. A symbol with no snapshot today is read from the
    prior session's snapshots, evaluated as of that session, and marked
    ``stale``. A symbol is absent from the result only when it has snapshots
    on neither session (or, degenerately, no close of either kind to measure
    against). Without a bar today it keeps its reading, with realized vol and
    the comparisons built on it null. The regime's
    fallback compares against the median IV/VIX ratio of the symbols read in
    this same call, so the book is one call, not one per name.
    """
    if not symbols:
        return {}
    # No snapshot on or before today means no reading on either session, so
    # the book reads as empty without consulting the calendar (a book whose
    # options were never pulled, or a session past the calendar's bounds).
    has_snapshot = conn.execute(
        _ANY_SNAPSHOT, {"symbols": symbols, "session_date": session_date}
    ).scalar()
    if not has_snapshot:
        return {}

    prior = previous_session(session_date)
    chains = _read_chains(conn, symbols, session_date)
    prior_chains = _read_chains(conn, symbols, prior)
    bars = _read_bars(conn, symbols, session_date)
    history = _read_history(conn, user_id, symbols, session_date)
    vix = _read_vix(conn, session_date)
    shares = _read_shares(conn, user_id, symbols, session_date)
    prior_strip = _read_prior_strip(conn, symbols, prior)

    vix_close = vix[-1][1] if vix and vix[-1][0] == session_date else None
    vix_pctile = percentile([c for _, c in vix]) if vix_close is not None else None

    readings: list[VolReading] = []
    for symbol in symbols:
        stale = symbol not in chains
        contracts = prior_chains.get(symbol) if stale else chains.get(symbol)
        if not contracts:
            continue
        symbol_bars = bars.get(symbol, [])
        has_bar = bool(symbol_bars) and symbol_bars[-1][0] == session_date
        chain_date = prior if stale else session_date
        cboe, payload_close = _read_underlying(conn, symbol, chain_date)
        spot = payload_close or next((c for d, c, _ in symbol_bars if d == chain_date), None)
        if spot is None:
            continue

        iv = iv30_from_chain(contracts, spot, chain_date)
        cells = select_strip(contracts, spot, chain_date)
        if stale:
            # The chain *is* the prior session's: there is no change to report.
            strip = strip_rows(cells, {}, {})
        else:
            prior_mids = {c.symbol: c.mid for c in prior_chains.get(symbol, []) if c.two_sided}
            strip = strip_rows(cells, prior_strip.get(symbol, {}), prior_mids)

        # realized_vol treats the last close as today's; without one, no HV.
        adj = [a for _, _, a in symbol_bars] if has_bar else []
        hv20, hv60 = realized_vol(adj, 20), realized_vol(adj, 60)
        iv30 = iv.iv30
        iv30_hist = history.get((symbol, "iv30"), [])
        ratio_hist = history.get((symbol, "iv_vix_ratio"), [])
        iv_vix_ratio = iv30 / vix_close if iv30 is not None and vix_close else None
        if iv30 is not None and cboe is not None and abs(iv30 - cboe) > IV30_DRIFT_ALARM_POINTS:
            print(f"volatility {session_date} {symbol}: iv30 {iv30:.2f} vs cboe {cboe:.2f}")

        far = iv.far.expiry if iv.far is not None else session_date
        horizon = max(far, session_date + timedelta(days=EARNINGS_WINDOW_MIN_DAYS))
        readings.append(
            VolReading(
                symbol=symbol,
                close=symbol_bars[-1][1] if stale and has_bar else spot,
                iv30=iv30,
                iv30_n=iv.n,
                iv30_cboe=cboe,
                iv30_thin=iv.n < IV30_MIN_CONTRACTS,
                iv30_pctile=percentile([*iv30_hist, iv30]) if iv30 is not None else None,
                iv_rank_252=_rank(iv30_hist, iv30),
                hv20=hv20,
                hv60=hv60,
                iv_hv_spread=iv30 - hv20 if iv30 is not None and hv20 is not None else None,
                iv_hv_ratio=iv30 / hv20 if iv30 is not None and hv20 else None,
                iv_vix_ratio=iv_vix_ratio,
                iv_vix_ratio_pctile=(
                    percentile([*ratio_hist, iv_vix_ratio]) if iv_vix_ratio is not None else None
                ),
                vix_close=vix_close,
                vix_pctile=vix_pctile,
                iv_term_slope=term_slope(cells),
                premium="fair",  # set below, once the book's median ratio is known
                earnings_date=_read_earnings(conn, symbol, session_date, horizon),
                contracts_writable=int(shares.get(symbol, Decimal(0)) // 100),
                stale=stale,
                strip=tuple(strip),
            )
        )

    ratios = [r.iv_vix_ratio for r in readings if r.iv_vix_ratio is not None]
    book_median_ratio = median(ratios) if ratios else None
    out = {
        r.symbol: replace(
            r,
            premium=regime(
                iv30_pctile=r.iv30_pctile,
                iv_hv_spread=r.iv_hv_spread,
                iv30_thin=r.iv30_thin,
                iv_vix_ratio=r.iv_vix_ratio,
                book_median_ratio=book_median_ratio,
            ),
        )
        for r in readings
    }
    _store(conn, user_id, session_date, out)
    return out


def _rank(history: list[Decimal], today: Decimal | None) -> Decimal | None:
    """``(today − min) / (max − min)`` over the full 252-session baseline with
    today included, so the rank stays in [0, 1]; ``None`` below a full
    baseline, the percentile's rule, or when the year never moved."""
    if today is None or len(history) < BASELINE_SESSIONS:
        return None
    window = [*history[-BASELINE_SESSIONS:], today]
    lo, hi = min(window), max(window)
    if hi == lo:
        return None
    return (today - lo) / (hi - lo)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _read_chains(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, list[Contract]]:
    """Stored snapshots rebuilt as ``Contract``s, keyed by underlying."""
    out: dict[str, list[Contract]] = {}
    for row in conn.execute(
        _READ_CHAINS, {"symbols": symbols, "session_date": session_date}
    ).mappings():
        bid, ask, strike = _dec(row["bid"]), _dec(row["ask"]), _dec(row["strike"])
        assert bid is not None and ask is not None and strike is not None  # NOT NULL
        out.setdefault(str(row["underlying"]), []).append(
            Contract(
                symbol=str(row["contract"]),
                expiry=row["expiration_date"],
                right=str(row["put_or_call"]),
                strike=strike,
                bid=bid,
                ask=ask,
                bid_size=int(row["bid_size"] or 0),
                ask_size=int(row["ask_size"] or 0),
                iv=_dec(row["iv"]),
                delta=_dec(row["delta"]),
                gamma=_dec(row["gamma"]),
                theta=_dec(row["theta"]),
                vega=_dec(row["vega"]),
                rho=_dec(row["rho"]),
                open_interest=int(row["open_interest"]),
                volume=int(row["volume"]),
                last=_dec(row["last"]),
                prev_day_close=_dec(row["prev_day_close"]),
                percent_change=None,  # not stored
            )
        )
    return out


def _read_bars(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, list[tuple[date, Decimal, Decimal]]]:
    """``(session_date, c, adj_c)`` per symbol, oldest first."""
    out: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
    for row in conn.execute(
        _READ_BARS, {"symbols": symbols, "session_date": session_date, "depth": _BAR_DEPTH}
    ).mappings():
        c, adj_c = _dec(row["c"]), _dec(row["adj_c"])
        assert c is not None and adj_c is not None  # NOT NULL
        out.setdefault(str(row["symbol"]), []).append((row["session_date"], c, adj_c))
    return out


def _read_history(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> dict[tuple[str, str], list[Decimal]]:
    out: dict[tuple[str, str], list[Decimal]] = {}
    for row in conn.execute(
        _READ_HISTORY,
        {
            "user_id": user_id,
            "symbols": symbols,
            "metrics": ["iv30", "iv_vix_ratio"],
            "session_date": session_date,
            "depth": BASELINE_SESSIONS,
        },
    ).mappings():
        value = _dec(row["value"])
        assert value is not None  # NOT NULL
        out.setdefault((str(row["symbol"]), str(row["metric"])), []).append(value)
    return out


def _read_vix(conn: Connection, session_date: date) -> list[tuple[date, Decimal]]:
    out: list[tuple[date, Decimal]] = []
    for row in conn.execute(
        _READ_VIX, {"session_date": session_date, "depth": _VIX_DEPTH}
    ).mappings():
        c = _dec(row["c"])
        assert c is not None  # NOT NULL
        out.append((row["session_date"], c))
    return out


def _read_shares(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> dict[str, Decimal]:
    rows = conn.execute(
        _READ_SHARES, {"user_id": user_id, "symbols": symbols, "session_date": session_date}
    ).mappings()
    return {str(r["symbol"]): Decimal(str(r["shares"])) for r in rows}


def _read_prior_strip(
    conn: Connection, symbols: list[str], session_date: date
) -> dict[str, dict[tuple[int, str], StripRow]]:
    """The prior session's strip, carrying only the two fields the changes read."""
    out: dict[str, dict[tuple[int, str], StripRow]] = {}
    for row in conn.execute(
        _READ_PRIOR_STRIP, {"symbols": symbols, "session_date": session_date}
    ).mappings():
        tenor, rule = int(row["tenor"]), str(row["strike_rule"])
        out.setdefault(str(row["symbol"]), {})[(tenor, rule)] = StripRow(
            tenor=tenor,
            rule=rule,
            contract=None,
            dte=None,
            moneyness=None,
            mid=None,
            premium_pct=_dec(row["premium_pct"]),
            time_value_pct=None,
            iv=_dec(row["iv"]),
            delta=None,
            open_interest=None,
            volume=None,
            chg_contract_pct=None,
            chg_tenor_pct=None,
            iv_chg=None,
            tenor_collapsed=False,
            target_miss=False,
        )
    return out


def _read_underlying(
    conn: Connection, symbol: str, as_of: date
) -> tuple[Decimal | None, Decimal | None]:
    """The vendor's own IV30 and the close, from the stored chain payload;
    ``(None, None)`` when there is no payload."""
    row = conn.execute(_READ_UNDERLYING, {"symbol": symbol, "as_of": as_of}).mappings().first()
    if row is None:
        return None, None
    return _dec(row["iv30"]), _dec(row["close"])


def _read_earnings(conn: Connection, symbol: str, session_date: date, horizon: date) -> date | None:
    found = conn.execute(
        _READ_EARNINGS, {"symbol": symbol, "session_date": session_date, "horizon": horizon}
    ).scalar()
    assert found is None or isinstance(found, date)
    return found


def _store(
    conn: Connection, user_id: str, session_date: date, readings: dict[str, VolReading]
) -> None:
    """The fourteen numeric metrics (``None`` skipped) and the strip. ``premium``
    is an enum and is not stored, M19's reason: it is recomputed by ``regime``
    from the stored numbers wherever it is needed."""
    metric_rows: list[dict[str, object]] = []
    strip_rows_out: list[dict[str, object]] = []
    for r in readings.values():
        values: dict[str, Decimal | None] = {
            "iv30": r.iv30,
            "iv30_n": Decimal(r.iv30_n),
            "iv30_cboe": r.iv30_cboe,
            "iv30_pctile": r.iv30_pctile,
            "iv_rank_252": r.iv_rank_252,
            "hv20": r.hv20,
            "hv60": r.hv60,
            "iv_hv_spread": r.iv_hv_spread,
            "iv_hv_ratio": r.iv_hv_ratio,
            "iv_vix_ratio": r.iv_vix_ratio,
            "iv_vix_ratio_pctile": r.iv_vix_ratio_pctile,
            "vix_pctile": r.vix_pctile,
            "iv_term_slope": r.iv_term_slope,
            "contracts_writable": Decimal(r.contracts_writable),
        }
        metric_rows.extend(
            {
                "user_id": user_id,
                "symbol": r.symbol,
                "session_date": session_date,
                "metric": metric,
                "value": value.quantize(_STORE_SCALE),
            }
            for metric, value in values.items()
            if value is not None
        )
        strip_rows_out.extend(
            {
                "symbol": r.symbol,
                "session_date": session_date,
                "tenor": row.tenor,
                "strike_rule": row.rule,
                "contract": row.contract,
                "dte": row.dte,
                "moneyness": row.moneyness,
                "mid": row.mid,
                "premium_pct": row.premium_pct,
                "time_value_pct": row.time_value_pct,
                "iv": row.iv,
                "delta": row.delta,
                "open_interest": row.open_interest,
                "volume": row.volume,
                "chg_contract_pct": row.chg_contract_pct,
                "chg_tenor_pct": row.chg_tenor_pct,
                "iv_chg": row.iv_chg,
                "tenor_collapsed": row.tenor_collapsed,
                "target_miss": row.target_miss,
            }
            for row in r.strip
        )
    if metric_rows:
        conn.execute(_UPSERT_METRIC, metric_rows)
    if strip_rows_out:
        conn.execute(_UPSERT_STRIP, strip_rows_out)


# --- the open brief's reads (M25) ---------------------------------------------

_REGIME_METRICS = ("iv30_n", "iv30_pctile", "iv_hv_spread", "iv_vix_ratio")

_READ_SESSION_METRICS = text("""
    SELECT symbol, metric, value FROM metrics
    WHERE user_id = :user_id AND symbol = ANY(:symbols)
      AND session_date = :session_date AND metric = ANY(:metrics)
""")

# Whether the book has any reading before the open session at all: a book
# whose options were never pulled reads as no change without consulting the
# calendar, `_ANY_SNAPSHOT`'s reason (a session past the calendar's bounds).
_ANY_READING = text("""
    SELECT EXISTS (
        SELECT 1 FROM metrics
        WHERE user_id = :user_id AND symbol = ANY(:symbols)
          AND session_date < :session_date AND metric = 'iv30_n'
    )
""")

_READ_VIX_PCTILE = text("""
    SELECT value FROM metrics
    WHERE user_id = :user_id AND symbol = ANY(:symbols)
      AND session_date = :session_date AND metric = 'vix_pctile'
    LIMIT 1
""")


def _session_regimes(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> dict[str, str]:
    """Each name's regime at one close, from its stored metrics. The fallback's
    book median is the median ``iv_vix_ratio`` across the names read here, the
    set ``compute_and_store_volatility`` used. A name with no ``iv30_n`` that
    session had no reading and is absent."""
    by_symbol: dict[str, dict[str, Decimal]] = {}
    for row in conn.execute(
        _READ_SESSION_METRICS,
        {
            "user_id": user_id,
            "symbols": symbols,
            "session_date": session_date,
            "metrics": list(_REGIME_METRICS),
        },
    ).mappings():
        by_symbol.setdefault(str(row["symbol"]), {})[str(row["metric"])] = Decimal(
            str(row["value"])
        )
    ratios = [v["iv_vix_ratio"] for v in by_symbol.values() if "iv_vix_ratio" in v]
    book_median_ratio = median(ratios) if ratios else None
    return {
        symbol: regime_from_metrics(
            values, book_median_ratio, iv30_thin=values["iv30_n"] < IV30_MIN_CONTRACTS
        )
        for symbol, values in by_symbol.items()
        if "iv30_n" in values
    }


def _any_reading(conn: Connection, user_id: str, symbols: list[str], session_date: date) -> bool:
    if not symbols:
        return False
    params = {"user_id": user_id, "symbols": symbols, "session_date": session_date}
    return bool(conn.execute(_ANY_READING, params).scalar())


def read_premium_changes(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> list[tuple[str, str, str]]:
    """``(symbol, premium_prior, premium)`` for each name whose regime differs
    between the last two closes before ``session_date`` — the open brief's
    strip, read from the prior close's metrics with no new ingest. A name
    without a reading on both closes has nothing to compare and is left out."""
    if not _any_reading(conn, user_id, symbols, session_date):
        return []
    last = previous_session(session_date)
    now = _session_regimes(conn, user_id, symbols, last)
    then = _session_regimes(conn, user_id, symbols, previous_session(last))
    return [
        (symbol, then[symbol], now[symbol])
        for symbol in sorted(now)
        if symbol in then and then[symbol] != now[symbol]
    ]


def read_vix_pctile(
    conn: Connection, user_id: str, symbols: list[str], session_date: date
) -> Decimal | None:
    """VIX against its own year at the last close before ``session_date``. It
    is one figure per session stored on every held name's row; absent (None)
    below a full baseline, since the percentile is never stored short."""
    if not _any_reading(conn, user_id, symbols, session_date):
        return None
    value = conn.execute(
        _READ_VIX_PCTILE,
        {"user_id": user_id, "symbols": symbols, "session_date": previous_session(session_date)},
    ).scalar()
    return _dec(value)
