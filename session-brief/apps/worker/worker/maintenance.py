"""M13 Task 7 — maintenance flags: theme_misfit + beta_instability.

Dashboard-only. These land in the ``flags`` TABLE (via ``flags.record_flags``,
the same upsert `flags.py` uses) so the dashboard can read them directly, but
they never enter the BriefObject's `flags[]` — assembly builds that list from
`compute.ComputeResult` via `flags.surface_flags`, which never reads this
table (docs/04 §4: "dashboard-only / not into the reader's face"). Not
rate-limited: unlike the weekly-capped concentration/correlation flags, these
are read fresh from the latest fit/signal each refit, so there's no cap to
enforce.

Sourced from the M12 econometrics tables (docs/03): `attribution_fits.
diagnostics->>'r2_collapsed'` (the two-factor model's R² floor breach) and
`attribution_signals.beta_drift_20d` (Layer 3's rolling beta drift).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.flags import FlagCandidate, record_flags

BETA_INSTABILITY_THRESHOLD = 0.3

_HELD_SYMBOLS = text("SELECT DISTINCT symbol FROM holdings WHERE user_id = :u")

_LATEST_FIT_DIAGNOSTICS = text("""
    SELECT DISTINCT ON (symbol)
           symbol, (diagnostics->>'r2_collapsed')::boolean AS r2_collapsed
    FROM attribution_fits
    WHERE model_version = :mv AND fit_date <= :d AND symbol = ANY(:syms)
    ORDER BY symbol, fit_date DESC
""")

_LATEST_SIGNALS = text("""
    SELECT DISTINCT ON (symbol) symbol, beta_drift_20d
    FROM attribution_signals
    WHERE model_version = :mv AND trade_date <= :d AND symbol = ANY(:syms)
    ORDER BY symbol, trade_date DESC
""")


# --- Pure predicates ---------------------------------------------------------


def theme_misfit_flag(symbol: str, r2_collapsed: bool) -> FlagCandidate | None:
    if not r2_collapsed:
        return None
    return FlagCandidate("theme_misfit", "info", symbol, None, None, "theme_misfit")


def beta_instability_flag(symbol: str, beta_drift: float | None) -> FlagCandidate | None:
    if beta_drift is None or abs(beta_drift) <= BETA_INSTABILITY_THRESHOLD:
        return None
    return FlagCandidate("beta_instability", "info", symbol, None, beta_drift,
                         "beta_instability")


# --- Database layer -----------------------------------------------------------


def surface_maintenance_flags(
    conn: Connection, user_id: str, session_date: date, model_version: int
) -> list[FlagCandidate]:
    """Per held name, the latest fit's `r2_collapsed` diagnostic and the latest
    `beta_drift_20d` signal — built into candidates and persisted straight to
    the `flags` table (no rate-limit, no BriefObject involvement)."""
    symbols = sorted({str(r[0]) for r in conn.execute(_HELD_SYMBOLS, {"u": user_id}).all()})
    if not symbols:
        return []

    candidates: list[FlagCandidate] = []
    for row in conn.execute(
        _LATEST_FIT_DIAGNOSTICS, {"mv": model_version, "d": session_date, "syms": symbols}
    ).mappings():
        c = theme_misfit_flag(str(row["symbol"]), bool(row["r2_collapsed"]))
        if c is not None:
            candidates.append(c)

    for row in conn.execute(
        _LATEST_SIGNALS, {"mv": model_version, "d": session_date, "syms": symbols}
    ).mappings():
        drift = row["beta_drift_20d"]
        c = beta_instability_flag(
            str(row["symbol"]), float(drift) if drift is not None else None
        )
        if c is not None:
            candidates.append(c)

    record_flags(conn, user_id, session_date, candidates)
    return candidates
