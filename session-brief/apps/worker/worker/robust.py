"""Robust linear-algebra primitives for attribution (M12). Pure numpy, off the
money path; callers quantize to Decimal at storage. No DB, clock, or network.

IRLS = iteratively reweighted least squares with Huber weights, hand-rolled to
avoid a scipy/statsmodels transitive chain (a dozen lines, fully unit-testable).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAD_C = 1.4826
HUBER_K = 1.345


def ewma_weights(n: int, half_life: float = 60.0) -> np.ndarray:
    """w_t = lambda^age, lambda = 0.5**(1/half_life). Oldest obs has the largest
    age; the most recent obs (index n-1) has age 0 and weight 1.0. Returned
    oldest->newest to align with a chronologically sorted series."""
    lam = 0.5 ** (1.0 / half_life)
    ages = np.arange(n - 1, -1, -1, dtype=float)
    return np.asarray(lam ** ages)


def huber_weights(resid: np.ndarray, scale: float, k: float = HUBER_K) -> np.ndarray:
    """Huber psi weights: 1 inside k*scale, k*scale/|resid| outside. A zero scale
    (perfect fit) means no outliers to downweight -> all ones."""
    if scale == 0:
        return np.ones_like(resid)
    a = np.abs(resid) / scale
    w = np.ones_like(resid, dtype=float)
    mask = a > k
    w[mask] = k / a[mask]
    return np.asarray(w)


def wls(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least squares: solve (X^T W X) b = X^T W y via the sqrt-weight
    trick and lstsq (stable for near-collinear designs)."""
    sw = np.sqrt(w)
    b, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
    return np.asarray(b)


def mad_scale(resid: np.ndarray) -> float:
    """1.4826 * MAD about the median — a robust stdev estimate. Stdev would be
    inflated by exactly the fat-tailed event days we are detecting (spec)."""
    med = np.median(resid)
    mad = np.median(np.abs(resid - med))
    return float(MAD_C * mad)


@dataclass(frozen=True)
class IRLSResult:
    beta: np.ndarray
    resid: np.ndarray
    weights: np.ndarray
    scale: float
    converged: bool
    iters: int


def irls_huber(
    X: np.ndarray, y: np.ndarray, prior_w: np.ndarray | None = None, *,
    k: float = HUBER_K, max_iter: int = 25, tol: float = 1e-6,
) -> IRLSResult:
    """Huber IRLS. `prior_w` (e.g. EWMA) multiplies the Huber weights each
    iteration, so recency and robustness compose. Convergence is capped; on
    non-convergence we return the last iterate with converged=False (spec)."""
    n = X.shape[0]
    base = np.ones(n) if prior_w is None else np.asarray(prior_w, dtype=float)
    beta = wls(X, y, base)
    resid = y - X @ beta
    scale = mad_scale(resid)
    w = base
    converged = False
    it = 0
    for _ in range(1, max_iter + 1):
        it += 1
        w = base if scale == 0 else base * huber_weights(resid, scale, k)
        new_beta = wls(X, y, w)
        resid = y - X @ new_beta
        scale = mad_scale(resid)
        if float(np.max(np.abs(new_beta - beta))) < tol:
            beta = new_beta
            converged = True
            break
        beta = new_beta
    return IRLSResult(beta=beta, resid=resid, weights=w, scale=scale, converged=converged, iters=it)


def beta_standard_errors(X: np.ndarray, resid: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted OLS coefficient standard errors: sqrt(diag(sigma2 * (X^T W X)^-1)),
    sigma2 = sum(w * resid^2) / (n - p). Uses pinv for near-singular designs."""
    n, p = X.shape
    dof = max(n - p, 1)
    sigma2 = float(np.sum(w * resid**2) / dof)
    xtwx = X.T @ (w[:, None] * X)
    cov = sigma2 * np.linalg.pinv(xtwx)
    return np.sqrt(np.abs(np.diag(cov)))


def durbin_watson(resid: np.ndarray) -> float:
    """Lag-1 residual autocorrelation read: sum((e_t - e_{t-1})^2) / sum(e_t^2).
    ~2 = no autocorrelation; ->0 = positive autocorrelation (look-ahead smell)."""
    denom = float(np.sum(resid**2))
    if denom == 0:
        return 2.0
    return float(np.sum(np.diff(resid) ** 2) / denom)


def condition_number(X: np.ndarray) -> float:
    """2-norm condition number of the design matrix — the direct collinearity
    read. High = 'market and theme are fighting' (spec)."""
    return float(np.linalg.cond(X))
