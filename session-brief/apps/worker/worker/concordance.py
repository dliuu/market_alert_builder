"""Event-concordance validation (M13 Task 8): a periodic REPORTABLE check, not
a per-brief gate (spec §6). It asks whether high-|resid_z| days coincide with
event days at a rate above chance, by comparing the observed coincidence rate
to a shuffled baseline (mean coincidence rate over random day-draws of the
same size). No live news feed exists yet, so event dates are grounded on the
`events` table (earnings/lockup/macro) and `index_events` (docs/07 D25) —
both real tables, not synthetic fixtures.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

CONCORDANCE_Z = 2.0


def concordance_rate(high_days: set[date], event_days: set[date]) -> float:
    """Fraction of high-residual days that coincide with an event day. Empty
    high_days is defined as 0.0 (no division by zero)."""
    if not high_days:
        return 0.0
    return len(high_days & event_days) / len(high_days)


def shuffled_baseline(
    high_count: int, all_days: list[date], event_days: set[date], *, trials: int, seed: int
) -> float:
    """Mean coincidence rate when `high_count` days are drawn at random
    (without replacement) from `all_days`, over `trials` draws. Deterministic
    for a fixed seed via a local `random.Random` (never the global RNG)."""
    if high_count <= 0 or not all_days:
        return 0.0
    rng = random.Random(seed)
    total = 0.0
    for _ in range(trials):
        draw = set(rng.sample(all_days, min(high_count, len(all_days))))
        total += concordance_rate(draw, event_days)
    return total / trials


@dataclass(frozen=True)
class ConcordanceReport:
    observed_rate: float
    baseline_rate: float
    lift: float
    n_high: int
    n_events: int


def _report(
    high_days: set[date], all_days: list[date], event_days: set[date], *, trials: int, seed: int
) -> ConcordanceReport:
    observed = concordance_rate(high_days, event_days)
    baseline = shuffled_baseline(len(high_days), all_days, event_days, trials=trials, seed=seed)
    lift = observed / baseline if baseline != 0 else 0.0
    return ConcordanceReport(
        observed_rate=observed,
        baseline_rate=baseline,
        lift=lift,
        n_high=len(high_days),
        n_events=len(event_days),
    )


# --- Database layer ----------------------------------------------------------

_RESID_Z = text("""
    SELECT DISTINCT trade_date, resid_z FROM attribution
    WHERE trade_date BETWEEN :start AND :end AND model_version = :mv AND resid_z IS NOT NULL
""")

_EVENTS = text("""
    SELECT occurs_at AS d FROM events WHERE occurs_at BETWEEN :start AND :end
""")

_INDEX_EVENTS = text("""
    SELECT trade_date AS d FROM index_events WHERE trade_date BETWEEN :start AND :end
""")


def report_concordance(
    conn: Connection, start: date, end: date, *, model_version: int,
    z: float = CONCORDANCE_Z, trials: int = 1000, seed: int = 0,
) -> ConcordanceReport:
    """Reads `attribution.resid_z` over [start, end] for `model_version` to get
    all_days and high_days = {d : |resid_z| >= z}, and event dates from
    `events` + `index_events` (unioned), then reports observed vs shuffled-
    baseline concordance."""
    all_days: set[date] = set()
    high_days: set[date] = set()
    for row in conn.execute(_RESID_Z, {"start": start, "end": end, "mv": model_version}).mappings():
        d = row["trade_date"]
        all_days.add(d)
        if abs(float(row["resid_z"])) >= z:
            high_days.add(d)

    event_days: set[date] = set()
    for sql in (_EVENTS, _INDEX_EVENTS):
        for row in conn.execute(sql, {"start": start, "end": end}).mappings():
            event_days.add(row["d"])

    return _report(high_days, sorted(all_days), event_days, trials=trials, seed=seed)
