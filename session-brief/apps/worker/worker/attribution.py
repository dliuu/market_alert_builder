"""Pure two-factor OLS attribution (M11 walking skeleton).

Model: r_x = alpha + beta_m * r_market + beta_theme * r_basket_LOO + eps.
Raw theme, NO orthogonalization — market and theme are ~0.85 correlated, so the
betas are known-unstable here; that instability is the motivation for M12, not a
bug (spec). Float, off the money path; the DB layer quantizes to Decimal at
storage. Decomposition is additive by construction: resid is the closing term so
market+theme+resid == total exactly (the invariant-3 analogue for attribution).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.baskets import (
    equal_weight_return,
    leave_one_out,
    members_on,
    primary_theme_of,
    read_theme_members,
    upsert_basket_return,
)
from worker.compute import _STORE_SCALE
from worker.constants import BENCHMARK_SYMBOL
from worker.robust import (
    beta_standard_errors,
    condition_number,
    durbin_watson,
    ewma_weights,
    irls_huber,
    mad_scale,
)

FIT_WINDOW = 120
COLD_START_FLOOR = 40
BPS = 10_000
HALF_LIFE = 60.0
R2_COLLAPSE_FLOOR = 0.05


@dataclass(frozen=True)
class Fit:
    beta_market: float
    beta_theme: float
    alpha: float
    r2: float | None
    resid_scale: float | None  # plain stdev in M11; MAD is M12
    n_obs: int
    cold_start: bool


@dataclass(frozen=True)
class Decomposition:
    market_bps: float
    theme_bps: float
    resid_bps: float
    total_bps: float


def fit_ols(r_x: list[float], r_market: list[float], r_theme: list[float]) -> Fit:
    """Plain OLS of r_x on [1, r_market, r_theme] via the 3x3 normal equations.
    All days included (EWMA / Huber / contaminated-day exclusion are M12)."""
    n = len(r_x)
    if not (n == len(r_market) == len(r_theme)):
        raise ValueError("mismatched series lengths")
    if n < 2:
        raise ValueError("need at least 2 observations to fit")

    # Design columns: 1, m, t. Build X^T X (symmetric 3x3) and X^T y.
    s1, sm, st = float(n), sum(r_market), sum(r_theme)
    smm = sum(m * m for m in r_market)
    smt = sum(m * t for m, t in zip(r_market, r_theme, strict=True))
    stt = sum(t * t for t in r_theme)
    sy = sum(r_x)
    smy = sum(m * y for m, y in zip(r_market, r_x, strict=True))
    sty = sum(t * y for t, y in zip(r_theme, r_x, strict=True))

    ata = [[s1, sm, st], [sm, smm, smt], [st, smt, stt]]
    aty = [sy, smy, sty]
    alpha, beta_market, beta_theme = _solve3(ata, aty)

    resid = [
        y - (alpha + beta_market * m + beta_theme * t)
        for y, m, t in zip(r_x, r_market, r_theme, strict=True)
    ]
    mean_y = sy / n
    ss_tot = sum((y - mean_y) ** 2 for y in r_x)
    ss_res = sum(e * e for e in resid)
    r2 = None if ss_tot == 0 else 1.0 - ss_res / ss_tot
    resid_scale = math.sqrt(ss_res / (n - 1)) if n > 1 else None

    return Fit(
        beta_market=beta_market,
        beta_theme=beta_theme,
        alpha=alpha,
        r2=r2,
        resid_scale=resid_scale,
        n_obs=n,
        cold_start=n < COLD_START_FLOOR,
    )


def apply_cold_start(fit: Fit, theme_median_beta: float) -> Fit:
    """Shrink beta_theme toward the theme-median beta when the fit is thin:
    beta = w*beta_ols + (1-w)*beta_median, w = min(1, n_obs/FIT_WINDOW).
    Below the floor we never emit a confident residual from thin data (spec)."""
    if not fit.cold_start:
        return fit
    w = min(1.0, fit.n_obs / FIT_WINDOW)
    return replace(fit, beta_theme=w * fit.beta_theme + (1.0 - w) * theme_median_beta)


def decompose(fit: Fit, r_market: float, r_theme: float, r_x: float) -> Decomposition:
    """Additive decomposition of one day's move, in bps. resid is the closing
    term (total - market - theme) so the parts sum to total exactly."""
    total = r_x * BPS
    market = fit.beta_market * r_market * BPS
    theme = fit.beta_theme * r_theme * BPS
    resid = total - market - theme
    return Decomposition(market_bps=market, theme_bps=theme, resid_bps=resid, total_bps=total)


@dataclass(frozen=True)
class TwoStageFit:
    alpha: float
    beta_market: float
    beta_theta: float
    a: float                 # stage-A intercept: r_basket = a + b*r_market + rho
    b: float                 # stage-A market slope
    r2: float | None
    resid_scale: float | None  # MAD scale (1.4826 * MAD)
    beta_se: tuple[float, float, float]
    durbin_watson: float
    cond_number: float
    huber_converged: bool
    r2_collapsed: bool
    n_obs: int
    cold_start: bool


def orthogonalize(
    r_basket: np.ndarray | list[float], r_market: np.ndarray | list[float],
    w: np.ndarray,
) -> tuple[float, float, np.ndarray]:
    """Stage A: residualize the basket on the market (robust, EWMA-weighted).
    Returns (a, b, rho) where rho = r_basket - (a + b*r_market) is theme movement
    beyond the market. rho is orthogonal to r_market by construction of the WLS
    normal equations, so beta_theta stops splitting shared variance arbitrarily."""
    rb = np.asarray(r_basket, dtype=float)
    rm = np.asarray(r_market, dtype=float)
    X = np.column_stack([np.ones_like(rm), rm])
    res = irls_huber(X, rb, prior_w=w)
    a, b = float(res.beta[0]), float(res.beta[1])
    rho = rb - (a + b * rm)
    return a, b, rho


def fit_two_stage(
    r_x: list[float], r_market: list[float], r_basket: list[float], *,
    half_life: float = HALF_LIFE,
) -> TwoStageFit:
    """Two-stage sequential-orthogonalization fit with robust Huber/EWMA IRLS.
    Series are chronological (oldest first) so EWMA recency aligns. Additive
    decomposition: market = beta_m*r_market, theme = beta_theta*rho, resid = eps."""
    n = len(r_x)
    if not (n == len(r_market) == len(r_basket)):
        raise ValueError("mismatched series lengths")
    if n < 2:
        raise ValueError("need at least 2 observations to fit")

    y = np.asarray(r_x, dtype=float)
    rm = np.asarray(r_market, dtype=float)
    w = ewma_weights(n, half_life)

    a, b, rho = orthogonalize(r_basket, rm, w)
    X = np.column_stack([np.ones(n), rm, rho])
    res = irls_huber(X, y, prior_w=w)
    alpha, beta_market, beta_theta = (float(v) for v in res.beta)

    resid = res.resid
    mean_y = float(np.mean(y))
    ss_tot = float(np.sum((y - mean_y) ** 2))
    ss_res = float(np.sum(resid**2))
    r2 = None if ss_tot == 0 else 1.0 - ss_res / ss_tot
    se = beta_standard_errors(X, resid, res.weights)

    return TwoStageFit(
        alpha=alpha,
        beta_market=beta_market,
        beta_theta=beta_theta,
        a=a,
        b=b,
        r2=r2,
        resid_scale=mad_scale(resid),
        beta_se=(float(se[0]), float(se[1]), float(se[2])),
        durbin_watson=durbin_watson(resid),
        cond_number=condition_number(X),
        huber_converged=res.converged,
        r2_collapsed=(r2 is not None and r2 < R2_COLLAPSE_FLOOR),
        n_obs=n,
        cold_start=n < COLD_START_FLOOR,
    )


def decompose_ortho(
    fit: TwoStageFit, r_market: float, r_basket: float, r_x: float
) -> Decomposition:
    """Additive decomposition using stored stage-A (a, b) to reconstruct rho for
    this day. resid is the closing term so the parts sum to total exactly."""
    rho = r_basket - (fit.a + fit.b * r_market)
    total = r_x * BPS
    market = fit.beta_market * r_market * BPS
    theme = fit.beta_theta * rho * BPS
    resid = total - market - theme
    return Decomposition(market_bps=market, theme_bps=theme, resid_bps=resid, total_bps=total)


def _solve3(a: list[list[float]], b: list[float]) -> tuple[float, float, float]:
    """Gaussian elimination with partial pivoting for a 3x3 system. Small and
    hand-rolled to keep the pure core dependency-free."""
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if m[pivot][col] == 0:
            raise ValueError("singular design matrix (degenerate factors)")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(3):
            if r == col:
                continue
            factor = m[r][col] / m[col][col]
            m[r] = [m[r][k] - factor * m[col][k] for k in range(4)]
    return (m[0][3] / m[0][0], m[1][3] / m[1][1], m[2][3] / m[2][2])


# --- Database layer -------------------------------------------------------

# Trailing calendar days to scan for the FIT_WINDOW trading sessions. 120
# sessions is ~168 calendar days; 400 leaves generous room for holidays/gaps.
_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class RefitResult:
    fits_written: int
    symbols: list[str]
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreResult:
    rows_written: int
    skipped: list[str] = field(default_factory=list)


_SCORED_UNIVERSE = text("""
    SELECT DISTINCT h.symbol AS symbol
    FROM lots l JOIN holdings h ON h.id = l.holding_id
    WHERE l.opened_on <= :d AND (l.closed_on IS NULL OR l.closed_on > :d)
""")

_WINDOW_RETURNS = text("""
    SELECT symbol, session_date, adj_c / prev_c - 1 AS ret FROM (
        SELECT symbol, session_date, adj_c,
               LAG(adj_c) OVER (PARTITION BY symbol ORDER BY session_date) AS prev_c
        FROM bars_daily
        WHERE symbol = ANY(:syms) AND session_date <= :fit_date
    ) r
    WHERE prev_c IS NOT NULL AND prev_c <> 0 AND session_date > :start
    ORDER BY symbol, session_date
""")

_DAY_RETURNS = text("""
    SELECT symbol, adj_c / prev_c - 1 AS ret FROM (
        SELECT symbol, session_date, adj_c,
               LAG(adj_c) OVER (PARTITION BY symbol ORDER BY session_date) AS prev_c
        FROM bars_daily
        WHERE symbol = ANY(:syms)
    ) r
    WHERE session_date = :d AND prev_c IS NOT NULL AND prev_c <> 0
""")

_LATEST_FITS = text("""
    SELECT DISTINCT ON (symbol)
           symbol, beta_market, beta_theme, r2, n_obs, cold_start
    FROM attribution_fits
    WHERE model_version = :mv AND fit_date <= :d
    ORDER BY symbol, fit_date DESC
""")

_UPSERT_FIT = text("""
    INSERT INTO attribution_fits
      (symbol, model_version, fit_date, window_start, window_end,
       beta_market, beta_theme, alpha, r2, resid_scale, n_obs, cold_start, diagnostics)
    VALUES (:symbol, :mv, :fit_date, :window_start, :window_end,
       :beta_market, :beta_theme, :alpha, :r2, :resid_scale, :n_obs, :cold_start,
       '{}'::jsonb)
    ON CONFLICT (symbol, model_version, fit_date) DO UPDATE SET
       window_start=EXCLUDED.window_start, window_end=EXCLUDED.window_end,
       beta_market=EXCLUDED.beta_market, beta_theme=EXCLUDED.beta_theme,
       alpha=EXCLUDED.alpha, r2=EXCLUDED.r2, resid_scale=EXCLUDED.resid_scale,
       n_obs=EXCLUDED.n_obs, cold_start=EXCLUDED.cold_start
""")

_UPSERT_ATTR = text("""
    INSERT INTO attribution
      (symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps,
       beta_market, beta_theme, r2, n_obs, provisional, cold_start, synthetic, revised,
       computed_at)
    VALUES (:symbol, :trade_date, :mv, :market_bps, :theme_bps, :resid_bps, :total_bps,
       :beta_market, :beta_theme, :r2, :n_obs, :provisional, :cold_start, :synthetic,
       :revised, :computed_at)
    ON CONFLICT (symbol, trade_date, model_version) DO UPDATE SET
       market_bps=EXCLUDED.market_bps, theme_bps=EXCLUDED.theme_bps,
       resid_bps=EXCLUDED.resid_bps, total_bps=EXCLUDED.total_bps,
       beta_market=EXCLUDED.beta_market, beta_theme=EXCLUDED.beta_theme, r2=EXCLUDED.r2,
       n_obs=EXCLUDED.n_obs, provisional=EXCLUDED.provisional, cold_start=EXCLUDED.cold_start,
       synthetic=EXCLUDED.synthetic, revised=EXCLUDED.revised, computed_at=EXCLUDED.computed_at
""")


def _q(value: float | None) -> Decimal | None:
    """Quantize a float to the Decimal store scale (reuse compute's convention)."""
    if value is None:
        return None
    return Decimal(repr(value)).quantize(_STORE_SCALE, rounding=ROUND_HALF_UP)


def _scored_universe(conn: Connection, on: date, member_symbols: set[str]) -> list[str]:
    """Union of every user's held symbols with all theme members (basket-member
    bars must be scored too, so their returns exist). Sorted for determinism."""
    held = set(conn.execute(_SCORED_UNIVERSE, {"d": on}).scalars().all())
    return sorted(held | member_symbols)


def _read_window_returns(
    conn: Connection, symbols: list[str], fit_date: date
) -> dict[str, dict[date, float]]:
    start = fit_date - timedelta(days=_LOOKBACK_DAYS)
    out: dict[str, dict[date, float]] = {}
    rows = conn.execute(
        _WINDOW_RETURNS, {"syms": symbols, "fit_date": fit_date, "start": start}
    ).mappings()
    for row in rows:
        out.setdefault(row["symbol"], {})[row["session_date"]] = float(row["ret"])
    return out


def refit(
    conn: Connection, fit_date: date, *, now_utc: datetime, model_version: int
) -> RefitResult:
    """Fit beta over the trailing FIT_WINDOW sessions for every scored symbol on
    its is_primary theme's leave-one-out basket, and persist attribution_fits +
    basket_returns for the window. Injected clock (now_utc unused today but kept
    for the scheduler-ready signature). Symbols with no theme or too few
    observations are skipped, not fatal."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}
    scored = _scored_universe(conn, fit_date, member_symbols)

    needed = sorted(set(scored) | member_symbols | {BENCHMARK_SYMBOL})
    returns = _read_window_returns(conn, needed, fit_date)
    market = returns.get(BENCHMARK_SYMBOL, {})

    # Only build baskets for themes that actually back a scored symbol.
    needed_themes = {
        t for s in scored if (t := primary_theme_of(by_theme, s, fit_date)) is not None
    }
    # basket[theme_id][day] = (full_ret, n_members); also persisted to basket_returns.
    basket: dict[str, dict[date, tuple[float, int]]] = {}
    for tid in needed_themes:
        members = by_theme[tid]
        day_map: dict[date, tuple[float, int]] = {}
        days = {d for m in members for d in returns.get(m.symbol, {})}
        for day in days:
            live = members_on(members, day)
            rets = {s: returns[s][day] for s in live if day in returns.get(s, {})}
            if not rets:
                continue
            br = equal_weight_return(rets)
            day_map[day] = (br.ret, br.n_members)
            upsert_basket_return(
                conn, tid, day, model_version, br, synthetic=False, revised=False
            )
        basket[tid] = day_map

    # First pass: fit each scored symbol; collect non-cold betas per theme.
    pending: list[tuple[str, str, Fit, date, date]] = []
    skipped: list[str] = []
    nonclod: dict[str, list[float]] = {}
    for symbol in scored:
        theme_id = primary_theme_of(by_theme, symbol, fit_date)
        if theme_id is None:
            skipped.append(symbol)
            continue
        r_sym = returns.get(symbol, {})
        theme_days = basket.get(theme_id, {})
        common = sorted(set(r_sym) & set(market) & set(theme_days))
        common = common[-FIT_WINDOW:]
        if len(common) < 2:
            skipped.append(symbol)
            continue

        r_x, r_m, r_t = [], [], []
        members = by_theme[theme_id]
        for day in common:
            full, n = theme_days[day]
            is_member = symbol in members_on(members, day)
            if is_member and n >= 2:
                r_theme = leave_one_out(full, n, r_sym[day])
            else:
                r_theme = full
            r_x.append(r_sym[day])
            r_m.append(market[day])
            r_t.append(r_theme)

        fit = fit_ols(r_x, r_m, r_t)
        pending.append((symbol, theme_id, fit, common[0], common[-1]))
        if not fit.cold_start:
            nonclod.setdefault(theme_id, []).append(fit.beta_theme)

    # Second pass: shrink cold-start fits toward the theme median, then persist.
    written = 0
    for symbol, theme_id, fit, w_start, w_end in pending:
        if fit.cold_start:
            medians = nonclod.get(theme_id, [])
            median_beta = statistics.median(medians) if medians else 0.0
            fit = apply_cold_start(fit, median_beta)
        conn.execute(_UPSERT_FIT, {
            "symbol": symbol, "mv": model_version, "fit_date": fit_date,
            "window_start": w_start, "window_end": w_end,
            "beta_market": _q(fit.beta_market), "beta_theme": _q(fit.beta_theme),
            "alpha": _q(fit.alpha), "r2": _q(fit.r2), "resid_scale": _q(fit.resid_scale),
            "n_obs": fit.n_obs, "cold_start": fit.cold_start,
        })
        written += 1

    return RefitResult(fits_written=written, symbols=[p[0] for p in pending], skipped=skipped)


def score(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int,
    synthetic: bool,
) -> ScoreResult:
    """Decompose each scored name's move on trade_date using its latest fit and
    its is_primary theme's leave-one-out basket, and upsert attribution rows
    (provisional = synthetic). Reads official adj_c returns; the synthetic flag
    is metadata carried from the PM run and cleared at AM reconcile."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}

    fits = {
        row["symbol"]: row
        for row in conn.execute(
            _LATEST_FITS, {"mv": model_version, "d": trade_date}
        ).mappings()
    }
    needed = sorted(set(fits) | member_symbols | {BENCHMARK_SYMBOL})
    day_returns = {
        row["symbol"]: float(row["ret"])
        for row in conn.execute(_DAY_RETURNS, {"syms": needed, "d": trade_date}).mappings()
    }
    market_ret = day_returns.get(BENCHMARK_SYMBOL)

    # Daily basket per theme that backs a fitted symbol.
    needed_themes = {
        t for s in fits
        if (t := primary_theme_of(by_theme, s, trade_date)) is not None
    }
    basket: dict[str, tuple[float, int]] = {}
    for tid in needed_themes:
        members = by_theme[tid]
        live = members_on(members, trade_date)
        rets = {s: day_returns[s] for s in live if s in day_returns}
        if not rets:
            continue
        br = equal_weight_return(rets)
        basket[tid] = (br.ret, br.n_members)
        upsert_basket_return(
            conn, tid, trade_date, model_version, br,
            synthetic=synthetic, revised=False,
        )

    written = 0
    skipped: list[str] = []
    for symbol, frow in fits.items():
        r_x = day_returns.get(symbol)
        theme_id = primary_theme_of(by_theme, symbol, trade_date)
        if r_x is None or market_ret is None or theme_id is None or theme_id not in basket:
            skipped.append(symbol)
            continue
        full, n = basket[theme_id]
        is_member = symbol in members_on(by_theme[theme_id], trade_date)
        if is_member and n >= 2:
            r_theme = leave_one_out(full, n, r_x)
        elif is_member and n < 2:
            skipped.append(symbol)
            continue
        else:
            r_theme = full

        fit = Fit(
            beta_market=float(frow["beta_market"]),
            beta_theme=float(frow["beta_theme"]),
            alpha=0.0,
            r2=float(frow["r2"]) if frow["r2"] is not None else None,
            resid_scale=None,
            n_obs=int(frow["n_obs"]),
            cold_start=bool(frow["cold_start"]),
        )
        d = decompose(fit, market_ret, r_theme, r_x)
        conn.execute(_UPSERT_ATTR, {
            "symbol": symbol, "trade_date": trade_date, "mv": model_version,
            "market_bps": _q(d.market_bps), "theme_bps": _q(d.theme_bps),
            "resid_bps": _q(d.resid_bps), "total_bps": _q(d.total_bps),
            "beta_market": _q(fit.beta_market), "beta_theme": _q(fit.beta_theme),
            "r2": _q(fit.r2), "n_obs": fit.n_obs,
            "provisional": synthetic, "cold_start": fit.cold_start,
            "synthetic": synthetic, "revised": False, "computed_at": now_utc,
        })
        written += 1

    return ScoreResult(rows_written=written, skipped=skipped)
