"""Pure two-factor OLS attribution (M11 walking skeleton).

Model: r_x = alpha + beta_m * r_market + beta_theme * r_basket_LOO + eps.
Raw theme, NO orthogonalization — market and theme are ~0.85 correlated, so the
betas are known-unstable here; that instability is the motivation for M12, not a
bug (spec). Float, off the money path; the DB layer quantizes to Decimal at
storage. Decomposition is additive by construction: resid is the closing term so
market+theme+resid == total exactly (the invariant-3 analogue for attribution).
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.baskets import (
    BASKET_CAP,
    MIN_DOLLAR_VOLUME,
    loo_weighted_return,
    members_on,
    primary_theme_of,
    read_theme_members,
    screen_and_cap,
    upsert_basket_loo_returns_many,
    upsert_basket_returns_many,
    weighted_return,
)
from worker.compute import _STORE_SCALE
from worker.constants import BENCHMARK_SYMBOL
from worker.exclusions import contaminated_days
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
    beyond the market. rho is orthogonal to r_market under the weighted (Huber/
    EWMA) inner product by construction of the WLS normal equations, so unweighted
    correlation is small-but-nonzero; beta_theta stops splitting shared variance
    arbitrarily."""
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
        resid_scale=mad_scale(resid) * BPS,
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

_WINDOW_LIQUIDITY = text("""
    SELECT symbol, session_date, (v * c) AS dv
    FROM bars_daily
    WHERE symbol = ANY(:syms) AND session_date > :start AND session_date <= :fit_date
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

_DAY_LIQUIDITY = text("""
    SELECT symbol, (v * c) AS dv FROM bars_daily
    WHERE symbol = ANY(:syms) AND session_date = :d
""")

_LATEST_FITS = text("""
    SELECT DISTINCT ON (symbol)
           symbol, beta_market, beta_theme, alpha, r2, resid_scale, n_obs,
           cold_start, diagnostics
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
       CAST(:diagnostics AS jsonb))
    ON CONFLICT (symbol, model_version, fit_date) DO UPDATE SET
       window_start=EXCLUDED.window_start, window_end=EXCLUDED.window_end,
       beta_market=EXCLUDED.beta_market, beta_theme=EXCLUDED.beta_theme,
       alpha=EXCLUDED.alpha, r2=EXCLUDED.r2, resid_scale=EXCLUDED.resid_scale,
       n_obs=EXCLUDED.n_obs, cold_start=EXCLUDED.cold_start, diagnostics=EXCLUDED.diagnostics
""")

_UPSERT_ATTR = text("""
    INSERT INTO attribution
      (symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps,
       resid_z, beta_market, beta_theme, r2, n_obs, provisional, cold_start,
       synthetic, revised, computed_at)
    VALUES (:symbol, :trade_date, :mv, :market_bps, :theme_bps, :resid_bps, :total_bps,
       :resid_z, :beta_market, :beta_theme, :r2, :n_obs, :provisional, :cold_start,
       :synthetic, :revised, :computed_at)
    ON CONFLICT (symbol, trade_date, model_version) DO UPDATE SET
       market_bps=EXCLUDED.market_bps, theme_bps=EXCLUDED.theme_bps,
       resid_bps=EXCLUDED.resid_bps, total_bps=EXCLUDED.total_bps, resid_z=EXCLUDED.resid_z,
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


def _read_window_liquidity(
    conn: Connection, symbols: list[str], fit_date: date
) -> dict[str, dict[date, float]]:
    start = fit_date - timedelta(days=_LOOKBACK_DAYS)
    out: dict[str, dict[date, float]] = {}
    rows = conn.execute(
        _WINDOW_LIQUIDITY, {"syms": symbols, "fit_date": fit_date, "start": start}
    ).mappings()
    for row in rows:
        out.setdefault(row["symbol"], {})[row["session_date"]] = float(row["dv"])
    return out


def refit(
    conn: Connection, fit_date: date, *, now_utc: datetime, model_version: int
) -> RefitResult:
    """Fit each scored symbol's two-stage orthogonalized model over its trailing
    FIT_WINDOW *fit-eligible* sessions (contaminated days excluded), on its
    is_primary theme's capped/liquidity-screened leave-one-out basket. Persists
    attribution_fits (with diagnostics + MAD resid_scale), basket_returns (full
    weighted series + weights), and basket_loo_returns. Injected clock; symbols
    with no theme or too few observations are skipped, not fatal."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}
    scored = _scored_universe(conn, fit_date, member_symbols)

    needed = sorted(set(scored) | member_symbols | {BENCHMARK_SYMBOL})
    returns = _read_window_returns(conn, needed, fit_date)
    liquidity = _read_window_liquidity(conn, needed, fit_date)
    market = returns.get(BENCHMARK_SYMBOL, {})

    start = fit_date - timedelta(days=_LOOKBACK_DAYS)
    contaminated = contaminated_days(conn, sorted(scored), start, fit_date)

    needed_themes = {
        t for s in scored if (t := primary_theme_of(by_theme, s, fit_date)) is not None
    }

    basket_rows: list[dict[str, Any]] = []
    loo_rows: list[dict[str, Any]] = []
    fit_rows: list[dict[str, Any]] = []

    # Full capped/screened basket per theme per day; persisted with its weights.
    basket_full: dict[str, dict[date, tuple[float, int]]] = {}
    for tid in needed_themes:
        members = by_theme[tid]
        day_map: dict[date, tuple[float, int]] = {}
        days = {d for m in members for d in returns.get(m.symbol, {})}
        for day in days:
            live = members_on(members, day)
            rets = {s: returns[s][day] for s in live if day in returns.get(s, {})}
            liq = {s: liquidity.get(s, {}).get(day, 0.0) for s in live}
            weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
            if not weights or not rets:
                continue
            br = weighted_return(rets, weights)
            day_map[day] = (br.ret, br.n_members)
            basket_rows.append({
                "theme_id": tid, "trade_date": day, "model_version": model_version,
                "ret": br.ret, "n_members": br.n_members,
                "synthetic": False, "revised": False, "weights": weights,
            })
        basket_full[tid] = day_map

    pending: list[tuple[str, str, TwoStageFit, date, date]] = []
    skipped: list[str] = []
    noncold: dict[str, list[float]] = {}
    for symbol in scored:
        theme_id = primary_theme_of(by_theme, symbol, fit_date)
        if theme_id is None:
            skipped.append(symbol)
            continue
        members = by_theme[theme_id]
        r_sym = returns.get(symbol, {})
        bad = contaminated.get(symbol, set())

        # Fit-eligible common days: symbol, market, and a basket value all present,
        # minus the symbol's contaminated days. Then take the trailing window.
        common = sorted(
            d for d in (set(r_sym) & set(market) & set(basket_full.get(theme_id, {})))
            if d not in bad
        )[-FIT_WINDOW:]
        if len(common) < 2:
            skipped.append(symbol)
            continue

        r_x, r_m, r_t = [], [], []
        for day in common:
            live = members_on(members, day)
            rets = {s: returns[s][day] for s in live if day in returns.get(s, {})}
            liq = {s: liquidity.get(s, {}).get(day, 0.0) for s in live}
            if symbol in rets:
                weights = screen_and_cap(
                    {s: dv for s, dv in liq.items() if s != symbol},
                    min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP,
                )
            else:
                weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
            if not weights or not rets:
                continue
            if symbol in rets:
                loo = loo_weighted_return(
                    rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME,
                    cap=BASKET_CAP, excluded=symbol,
                )
            else:
                loo = weighted_return(rets, weights)
            r_x.append(r_sym[day])
            r_m.append(market[day])
            r_t.append(loo.ret)
            loo_rows.append({
                "theme_id": theme_id, "excluded_symbol": symbol, "trade_date": day,
                "model_version": model_version, "ret": loo.ret, "n_members": loo.n_members,
            })

        fit = fit_two_stage(r_x, r_m, r_t)
        pending.append((symbol, theme_id, fit, common[0], common[-1]))
        if not fit.cold_start:
            noncold.setdefault(theme_id, []).append(fit.beta_theta)

    written = 0
    for symbol, theme_id, fit, w_start, w_end in pending:
        beta_theta = fit.beta_theta
        if fit.cold_start:
            medians = noncold.get(theme_id, [])
            median_beta = statistics.median(medians) if medians else 0.0
            w = min(1.0, fit.n_obs / FIT_WINDOW)
            beta_theta = w * fit.beta_theta + (1.0 - w) * median_beta
        diagnostics = {
            "a": fit.a, "b": fit.b,
            "beta_se": list(fit.beta_se),
            "resid_autocorr": fit.durbin_watson,
            "cond_number": fit.cond_number,
            "huber_converged": fit.huber_converged,
            "r2_collapsed": fit.r2_collapsed,
        }
        fit_rows.append({
            "symbol": symbol, "mv": model_version, "fit_date": fit_date,
            "window_start": w_start, "window_end": w_end,
            "beta_market": _q(fit.beta_market), "beta_theme": _q(beta_theta),
            "alpha": _q(fit.alpha), "r2": _q(fit.r2), "resid_scale": _q(fit.resid_scale),
            "n_obs": fit.n_obs, "cold_start": fit.cold_start,
            "diagnostics": json.dumps(diagnostics),
        })
        written += 1

    upsert_basket_returns_many(conn, basket_rows)
    upsert_basket_loo_returns_many(conn, loo_rows)
    if fit_rows:
        conn.execute(_UPSERT_FIT, fit_rows)

    return RefitResult(fits_written=written, symbols=[p[0] for p in pending], skipped=skipped)


def score(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int,
    synthetic: bool,
) -> ScoreResult:
    """Decompose each scored name's move on trade_date using its latest fit's
    stored stage-A (a, b) to reconstruct rho, and its is_primary theme's
    capped/screened leave-one-out basket. Writes resid_z = resid_bps / resid_scale
    (the MAD-scaled salience score M13 ranks on). Contaminated days are NOT
    excluded here — they are scored (the residual is the point)."""
    by_theme = read_theme_members(conn)
    member_symbols = {m.symbol for members in by_theme.values() for m in members}

    fits = {
        row["symbol"]: row
        for row in conn.execute(_LATEST_FITS, {"mv": model_version, "d": trade_date}).mappings()
    }
    needed = sorted(set(fits) | member_symbols | {BENCHMARK_SYMBOL})
    day_returns = {
        row["symbol"]: float(row["ret"])
        for row in conn.execute(_DAY_RETURNS, {"syms": needed, "d": trade_date}).mappings()
    }
    day_liquidity = {
        row["symbol"]: float(row["dv"])
        for row in conn.execute(_DAY_LIQUIDITY, {"syms": needed, "d": trade_date}).mappings()
    }
    market_ret = day_returns.get(BENCHMARK_SYMBOL)

    written = 0
    skipped: list[str] = []
    for symbol, frow in fits.items():
        r_x = day_returns.get(symbol)
        theme_id = primary_theme_of(by_theme, symbol, trade_date)
        if r_x is None or market_ret is None or theme_id is None:
            skipped.append(symbol)
            continue
        members = by_theme[theme_id]
        live = members_on(members, trade_date)
        rets = {s: day_returns[s] for s in live if s in day_returns}
        liq = {s: day_liquidity.get(s, 0.0) for s in live}
        if symbol in rets:
            weights = screen_and_cap(
                {s: dv for s, dv in liq.items() if s != symbol},
                min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP,
            )
            if not weights:
                skipped.append(symbol)
                continue
            loo = loo_weighted_return(
                rets, liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP, excluded=symbol,
            )
        else:
            weights = screen_and_cap(liq, min_dollar_volume=MIN_DOLLAR_VOLUME, cap=BASKET_CAP)
            if not weights:
                skipped.append(symbol)
                continue
            loo = weighted_return(rets, weights)

        diag = frow["diagnostics"] or {}
        fit = TwoStageFit(
            alpha=float(frow["alpha"]) if frow["alpha"] is not None else 0.0,
            beta_market=float(frow["beta_market"]),
            beta_theta=float(frow["beta_theme"]),
            a=float(diag.get("a", 0.0)), b=float(diag.get("b", 0.0)),
            r2=float(frow["r2"]) if frow["r2"] is not None else None,
            resid_scale=float(frow["resid_scale"]) if frow["resid_scale"] is not None else None,
            beta_se=(0.0, 0.0, 0.0), durbin_watson=0.0, cond_number=0.0,
            huber_converged=True, r2_collapsed=False,
            n_obs=int(frow["n_obs"]), cold_start=bool(frow["cold_start"]),
        )
        d = decompose_ortho(fit, market_ret, loo.ret, r_x)
        resid_z = None
        if fit.resid_scale not in (None, 0.0):
            resid_z = d.resid_bps / fit.resid_scale
        conn.execute(_UPSERT_ATTR, {
            "symbol": symbol, "trade_date": trade_date, "mv": model_version,
            "market_bps": _q(d.market_bps), "theme_bps": _q(d.theme_bps),
            "resid_bps": _q(d.resid_bps), "total_bps": _q(d.total_bps), "resid_z": _q(resid_z),
            "beta_market": _q(fit.beta_market), "beta_theme": _q(fit.beta_theta),
            "r2": _q(fit.r2), "n_obs": fit.n_obs,
            "provisional": synthetic, "cold_start": fit.cold_start,
            "synthetic": synthetic, "revised": False, "computed_at": now_utc,
        })
        written += 1

    return ScoreResult(rows_written=written, skipped=skipped)
