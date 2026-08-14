from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.constants import ATTRIBUTION_MODEL_VERSION as MV

D = date(2026, 1, 1)


def test_concordance_rate_counts_coincidence() -> None:
    from worker.concordance import concordance_rate

    high = {date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)}
    events = {date(2026, 1, 5), date(2026, 1, 6)}
    assert concordance_rate(high, events) == pytest.approx(2 / 3)


def test_concordance_rate_empty_high_days_is_zero() -> None:
    from worker.concordance import concordance_rate

    assert concordance_rate(set(), {date(2026, 1, 5)}) == 0.0


def test_planted_high_days_beat_shuffled_baseline() -> None:
    from worker.concordance import concordance_rate, shuffled_baseline

    all_days = [date(2026, 1, d) for d in range(1, 31)]
    events = {date(2026, 1, 3), date(2026, 1, 10), date(2026, 1, 20)}
    high = set(events)  # planted: every high-residual day is an event day
    observed = concordance_rate(high, events)
    baseline = shuffled_baseline(len(high), all_days, events, trials=2000, seed=0)
    assert observed == 1.0
    assert observed > baseline * 3  # well above chance


def test_shuffled_baseline_is_deterministic_for_a_fixed_seed() -> None:
    from worker.concordance import shuffled_baseline

    all_days = [date(2026, 1, d) for d in range(1, 31)]
    events = {date(2026, 1, 3), date(2026, 1, 10), date(2026, 1, 20)}
    a = shuffled_baseline(3, all_days, events, trials=500, seed=42)
    b = shuffled_baseline(3, all_days, events, trials=500, seed=42)
    assert a == b


def test_shuffled_baseline_draws_without_replacement() -> None:
    """high_count == len(all_days) must always draw every day exactly once, so
    the coincidence rate always equals the population's event fraction."""
    from worker.concordance import shuffled_baseline

    all_days = [date(2026, 1, d) for d in range(1, 6)]
    events = {date(2026, 1, 1), date(2026, 1, 2)}
    baseline = shuffled_baseline(len(all_days), all_days, events, trials=50, seed=7)
    assert baseline == pytest.approx(len(events) / len(all_days))


# --- ConcordanceReport ------------------------------------------------------


def test_concordance_report_lift_and_counts() -> None:
    from worker.concordance import ConcordanceReport

    report = ConcordanceReport(
        observed_rate=0.8, baseline_rate=0.2, lift=4.0, n_high=5, n_events=3
    )
    assert report.lift == pytest.approx(4.0)
    assert report.n_high == 5
    assert report.n_events == 3


# --- DB layer ----------------------------------------------------------------


def _insert_attribution(
    conn: Connection, symbol: str, trade_date: date, resid_z: float | None
) -> None:
    conn.execute(text("""
        INSERT INTO attribution
          (symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps,
           resid_z, beta_market, beta_theme, r2, n_obs, provisional, cold_start,
           synthetic, revised, computed_at)
        VALUES
          (:symbol, :d, :mv, 0, 0, 0, 0, :resid_z, 1, 1, 0.5, 100, false, false,
           false, false, :now)
    """), {
        "symbol": symbol, "d": trade_date, "mv": MV, "resid_z": resid_z,
        "now": datetime(2026, 1, 1, tzinfo=UTC),
    })


def test_report_concordance_planted_events_beat_baseline(db_conn: Connection) -> None:
    from worker.concordance import report_concordance

    start = date(2026, 1, 1)
    end = date(2026, 1, 30)
    all_days = [date(2026, 1, d) for d in range(1, 31)]
    event_days = [date(2026, 1, 3), date(2026, 1, 10), date(2026, 1, 20)]

    for d in event_days:
        conn = db_conn
        conn.execute(text(
            "INSERT INTO events (symbol, event_type, occurs_at) VALUES ('ZZZ', 'earnings', :d)"
        ), {"d": d})

    for d in all_days:
        resid_z = 3.5 if d in event_days else 0.1
        _insert_attribution(db_conn, "ZZZ", d, resid_z)

    report = report_concordance(db_conn, start, end, model_version=MV, trials=2000, seed=0)

    assert report.n_high == 3
    assert report.n_events == 3
    assert report.observed_rate == pytest.approx(1.0)
    assert report.lift > 1.0
    assert report.observed_rate > report.baseline_rate


def test_report_concordance_reads_index_events_too(db_conn: Connection) -> None:
    from worker.concordance import report_concordance

    start = date(2026, 1, 1)
    end = date(2026, 1, 10)
    high_day = date(2026, 1, 5)

    db_conn.execute(text(
        "INSERT INTO index_events (symbol, trade_date, index_key, effective_from) "
        "VALUES ('ZZZ', :d, 'sp500', :d)"
    ), {"d": high_day})

    for i in range(1, 11):
        d = date(2026, 1, i)
        _insert_attribution(db_conn, "ZZZ", d, 3.0 if d == high_day else 0.1)

    report = report_concordance(db_conn, start, end, model_version=MV, trials=500, seed=0)
    assert report.n_events == 1
    assert report.observed_rate == pytest.approx(1.0)


def test_report_concordance_uses_absolute_resid_z(db_conn: Connection) -> None:
    """A large-negative resid_z must count as 'high' too (|resid_z| >= z)."""
    from worker.concordance import report_concordance

    start = date(2026, 1, 1)
    end = date(2026, 1, 5)
    d = date(2026, 1, 3)
    db_conn.execute(text(
        "INSERT INTO events (symbol, event_type, occurs_at) VALUES ('ZZZ', 'earnings', :d)"
    ), {"d": d})
    for i in range(1, 6):
        day = date(2026, 1, i)
        _insert_attribution(db_conn, "ZZZ", day, -3.5 if day == d else 0.1)

    report = report_concordance(db_conn, start, end, model_version=MV, trials=200, seed=0)
    assert report.n_high == 1
    assert report.observed_rate == pytest.approx(1.0)
