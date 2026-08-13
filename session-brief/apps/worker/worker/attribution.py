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
from dataclasses import dataclass, replace

FIT_WINDOW = 120
COLD_START_FLOOR = 40
BPS = 10_000


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
    smt = sum(m * t for m, t in zip(r_market, r_theme))
    stt = sum(t * t for t in r_theme)
    sy = sum(r_x)
    smy = sum(m * y for m, y in zip(r_market, r_x))
    sty = sum(t * y for t, y in zip(r_theme, r_x))

    ata = [[s1, sm, st], [sm, smm, smt], [st, smt, stt]]
    aty = [sy, smy, sty]
    alpha, beta_market, beta_theme = _solve3(ata, aty)

    resid = [
        y - (alpha + beta_market * m + beta_theme * t)
        for y, m, t in zip(r_x, r_market, r_theme)
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
