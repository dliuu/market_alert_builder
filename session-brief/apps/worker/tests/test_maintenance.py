"""M13 Task 7 — maintenance flags: theme_misfit + beta_instability. Dashboard-only
(flags TABLE), never the BriefObject's flags[] (docs/07 D23/D-next)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.constants import ATTRIBUTION_MODEL_VERSION as MV

# --- Pure predicates ---------------------------------------------------------


def test_theme_misfit_fires_on_collapse() -> None:
    from worker.maintenance import theme_misfit_flag

    fired = theme_misfit_flag("MU", True)
    assert fired is not None
    assert fired.type == "theme_misfit"
    assert fired.severity == "info"
    assert fired.symbol == "MU"
    assert fired.text_key == "theme_misfit"
    assert theme_misfit_flag("MU", False) is None


def test_beta_instability_threshold() -> None:
    from worker.maintenance import beta_instability_flag

    fired = beta_instability_flag("MU", 0.35)
    assert fired is not None
    assert fired.type == "beta_instability"
    assert fired.severity == "info"
    assert fired.symbol == "MU"

    assert beta_instability_flag("MU", -0.4) is not None
    assert beta_instability_flag("MU", 0.29) is None
    assert beta_instability_flag("MU", None) is None


# --- DB: surface_maintenance_flags (DoD #5) ---------------------------------

_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fd"
_SECTOR = "ZZMAINT"


def _seed_user(conn: Connection) -> str:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-maint@example.invalid')"),
        {"u": _TEST_USER_ID},
    )
    return str(
        conn.execute(
            text("INSERT INTO sectors (user_id, name) VALUES (:u, :n) RETURNING id"),
            {"u": _TEST_USER_ID, "n": _SECTOR},
        ).scalar_one()
    )


def _seed_holding(conn: Connection, sector_id: str, symbol: str, opened_on: date) -> None:
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) "
            "VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": _TEST_USER_ID, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, 100, 9000, :o)"
        ),
        {"u": _TEST_USER_ID, "h": holding_id, "o": opened_on},
    )


def _seed_bar(conn: Connection, symbol: str, session_date: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": session_date, "c": Decimal(close)},
    )


def _seed_fit(conn: Connection, symbol: str, fit_date: date, *, r2_collapsed: bool) -> None:
    conn.execute(
        text(
            "INSERT INTO attribution_fits (symbol, model_version, fit_date, window_start, "
            "window_end, beta_market, beta_theme, alpha, r2, resid_scale, n_obs, cold_start, "
            "diagnostics) VALUES (:sym, :mv, :d, :d, :d, 1.0, 1.0, 0.0, 0.01, 10.0, 120, false, "
            "CAST(:diag AS jsonb))"
        ),
        {
            "sym": symbol,
            "mv": MV,
            "d": fit_date,
            "diag": '{"r2_collapsed": true}' if r2_collapsed else '{"r2_collapsed": false}',
        },
    )


def _seed_signal(conn: Connection, symbol: str, trade_date: date, beta_drift: float) -> None:
    conn.execute(
        text(
            "INSERT INTO attribution_signals (symbol, trade_date, model_version, "
            "beta_drift_20d) VALUES (:sym, :d, :mv, :drift)"
        ),
        {"sym": symbol, "d": trade_date, "mv": MV, "drift": beta_drift},
    )


def test_surface_maintenance_flags_theme_misfit_lands_in_table_not_brief(
    db_conn: Connection,
) -> None:
    from worker.assemble import assemble_and_store
    from worker.maintenance import surface_maintenance_flags

    sector_id = _seed_user(db_conn)
    prev, session = date(2099, 4, 1), date(2099, 4, 2)
    _seed_holding(db_conn, sector_id, "ZMA", prev)
    _seed_bar(db_conn, "ZMA", prev, "100")
    _seed_bar(db_conn, "ZMA", session, "110")  # +10% → full-tier mover → brief sends
    _seed_fit(db_conn, "ZMA", session, r2_collapsed=True)

    surfaced = surface_maintenance_flags(db_conn, _TEST_USER_ID, session, MV)
    assert any(c.type == "theme_misfit" and c.symbol == "ZMA" for c in surfaced)

    row = db_conn.execute(
        text(
            "SELECT flag_type FROM flags WHERE user_id = :u AND flag_type = 'theme_misfit' "
            "AND symbol = 'ZMA'"
        ),
        {"u": _TEST_USER_ID},
    ).scalar_one_or_none()
    assert row == "theme_misfit"

    obj = assemble_and_store(db_conn, _TEST_USER_ID, session, "close")
    assert obj is not None
    assert all(f.type.value != "theme_misfit" for f in obj.flags)


def test_surface_maintenance_flags_beta_instability(db_conn: Connection) -> None:
    from worker.maintenance import surface_maintenance_flags

    sector_id = _seed_user(db_conn)
    prev, session = date(2099, 5, 1), date(2099, 5, 2)
    _seed_holding(db_conn, sector_id, "ZMB", prev)
    _seed_signal(db_conn, "ZMB", session, 0.42)

    surfaced = surface_maintenance_flags(db_conn, _TEST_USER_ID, session, MV)
    assert any(c.type == "beta_instability" and c.symbol == "ZMB" for c in surfaced)

    row = db_conn.execute(
        text(
            "SELECT flag_type FROM flags WHERE user_id = :u AND flag_type = 'beta_instability' "
            "AND symbol = 'ZMB'"
        ),
        {"u": _TEST_USER_ID},
    ).scalar_one_or_none()
    assert row == "beta_instability"


def test_surface_maintenance_flags_no_held_names_is_noop(db_conn: Connection) -> None:
    from worker.maintenance import surface_maintenance_flags

    _seed_user(db_conn)
    surfaced = surface_maintenance_flags(db_conn, _TEST_USER_ID, date(2099, 5, 2), MV)
    assert surfaced == []
