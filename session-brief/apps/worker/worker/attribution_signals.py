"""Derived attribution signals (M12, Layer 3), computed weekly from stored fits
and residuals — cheap over data already persisted. Feed M13 consumers; computed
here as part of the scoring machinery. Float, off the money path; quantized at
storage via attribution._q."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.attribution import _q
from worker.baskets import members_on, read_theme_members

BETA_DRIFT_FITS = 4          # ~20 sessions of weekly fits
RESID_TRAIL = 20             # sessions for rolling alpha / momentum

_FIT_HISTORY = text("""
    SELECT symbol, fit_date, beta_theme FROM attribution_fits
    WHERE model_version = :mv AND fit_date <= :d
    ORDER BY symbol, fit_date DESC
""")

_RESID_TRAIL = text("""
    SELECT symbol, trade_date, resid_bps FROM attribution
    WHERE model_version = :mv AND trade_date <= :d
    ORDER BY symbol, trade_date DESC
""")

_DAY_RESID = text("""
    SELECT symbol, resid_bps FROM attribution
    WHERE model_version = :mv AND trade_date = :d
""")

_UPSERT_SIGNAL = text("""
    INSERT INTO attribution_signals
        (symbol, trade_date, model_version, beta_drift_20d, resid_momentum, rolling_alpha)
    VALUES (:symbol, :d, :mv, :drift, :momentum, :alpha)
    ON CONFLICT (symbol, trade_date, model_version) DO UPDATE SET
        beta_drift_20d=EXCLUDED.beta_drift_20d, resid_momentum=EXCLUDED.resid_momentum,
        rolling_alpha=EXCLUDED.rolling_alpha
""")

_UPSERT_DISPERSION = text("""
    INSERT INTO theme_dispersion (theme_id, trade_date, model_version, dispersion_mad)
    VALUES (:theme_id, :d, :mv, :mad)
    ON CONFLICT (theme_id, trade_date, model_version) DO UPDATE SET
        dispersion_mad=EXCLUDED.dispersion_mad
""")


@dataclass(frozen=True)
class SignalsResult:
    names_written: int
    themes_written: int


def _mad(xs: list[float]) -> float:
    med = statistics.median(xs)
    return 1.4826 * statistics.median([abs(x - med) for x in xs])


def compute_signals(
    conn: Connection, trade_date: date, *, now_utc: datetime, model_version: int
) -> SignalsResult:
    beta_hist: dict[str, list[float]] = {}
    for row in conn.execute(_FIT_HISTORY, {"mv": model_version, "d": trade_date}).mappings():
        beta_hist.setdefault(row["symbol"], []).append(float(row["beta_theme"]))

    resid_hist: dict[str, list[float]] = {}
    for row in conn.execute(_RESID_TRAIL, {"mv": model_version, "d": trade_date}).mappings():
        resid_hist.setdefault(row["symbol"], []).append(float(row["resid_bps"]))

    names = 0
    for symbol in sorted(set(beta_hist) | set(resid_hist)):
        betas = beta_hist.get(symbol, [])
        drift = betas[0] - betas[BETA_DRIFT_FITS] if len(betas) > BETA_DRIFT_FITS else None
        trail = resid_hist.get(symbol, [])[:RESID_TRAIL]
        alpha = statistics.fmean(trail) if trail else None
        momentum = statistics.fmean(trail[:5]) if len(trail) >= 5 else None
        conn.execute(_UPSERT_SIGNAL, {
            "symbol": symbol, "d": trade_date, "mv": model_version,
            "drift": _q(drift), "momentum": _q(momentum), "alpha": _q(alpha),
        })
        names += 1

    by_theme = read_theme_members(conn)
    day_resid = {
        row["symbol"]: float(row["resid_bps"])
        for row in conn.execute(_DAY_RESID, {"mv": model_version, "d": trade_date}).mappings()
    }
    themes = 0
    for theme_id, members in by_theme.items():
        live = members_on(members, trade_date)
        vals = [day_resid[s] for s in live if s in day_resid]
        if len(vals) < 2:
            continue
        conn.execute(_UPSERT_DISPERSION, {
            "theme_id": theme_id, "d": trade_date, "mv": model_version, "mad": _q(_mad(vals)),
        })
        themes += 1

    return SignalsResult(names_written=names, themes_written=themes)
