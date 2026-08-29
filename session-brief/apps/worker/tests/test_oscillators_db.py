"""M20 DB layer. Mirrors test_technicals_db.py's seeding style."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.oscillators import READ_DEPTH, compute_and_store_standing

USER = "00000000-0000-0000-0000-000000000001"

# Day-zero for the default (non-overlapping) window a single _seed_bars call
# produces. Pushed into the far future: ASTS and SPY carry real ingested
# bars_daily history in this shared dev database (2025-07-14 through
# 2026-08-28 as of writing), and the rolled-back transaction only protects
# rows *this* test inserts — it does nothing to shield a collision with rows
# that already exist. 2030 is well clear of any real feed.
_EPOCH = date(2030, 1, 5)


def _seed_bars(
    conn: Connection,
    symbol: str,
    n: int,
    *,
    adjusted: bool = True,
    start: str = "100",
    end: date | None = None,
) -> date:
    """n consecutive calendar-day bars ending at ``end`` (default: a window
    starting at ``_EPOCH``); returns the last session_date.

    ``end`` lets a second call extend a first call's window backward without
    colliding on the ``(symbol, session_date)`` primary key — needed by the
    "null bar anywhere in the window" test, which seeds ``READ_DEPTH - 1``
    good bars and then one bad bar that must land immediately *before* them,
    not restart at the same day-zero the first call used.

    ``adjusted=False`` leaves ``adj_c`` populated: the live schema has
    ``bars_daily.adj_c NOT NULL`` (only ``adj_o``/``adj_h``/``adj_l``/``adj_v``
    are nullable, added later by the M19 migration), so a row predating the
    adjusted-bar replay is missing the *other* adjusted columns, never adj_c
    itself. That is enough to trip the M19 null-check in ``_bars``.
    """
    end = end if end is not None else _EPOCH + timedelta(days=n - 1)
    first_day = end - timedelta(days=n - 1)
    price = Decimal(start)
    rows = []
    for i in range(n):
        price = price + Decimal(i % 7) / Decimal(4) + Decimal("0.1")
        span = (price * Decimal("0.005")).quantize(Decimal("0.01"))
        rows.append(
            {
                "s": symbol, "d": first_day + timedelta(days=i),
                "c": price, "h": price + span, "l": price - span, "v": 1_000_000,
                "adj_c": price,
                "adj_o": price if adjusted else None,
                "adj_h": (price + span) if adjusted else None,
                "adj_l": (price - span) if adjusted else None,
                "adj_v": 1_000_000 if adjusted else None,
            }
        )
    # ONE round trip. The db_conn fixture talks to a real remote Postgres —
    # tests/test_technicals_db.py takes 49s for five tests — and a loop of 286
    # single-row inserts per symbol would put this file into the minutes.
    conn.execute(
        text("""
            INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v,
                                    adj_c, adj_o, adj_h, adj_l, adj_v)
            VALUES (:s, :d, :c, :h, :l, :c, :v, :adj_c, :adj_o, :adj_h, :adj_l, :adj_v)
        """),
        rows,
    )
    return end


def test_full_window_yields_a_standing_with_percentiles(db_conn: Connection) -> None:
    last = _seed_bars(db_conn, "ZQZA", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    out = compute_and_store_standing(db_conn, USER, ["ZQZA"], last)
    assert "ZQZA" in out
    assert out["ZQZA"].rsi14_pctile is not None
    # ZQZA has no holdings row for this user, so it falls back to SPY — assert
    # it, so the SPY seed line above is exercised rather than dead weight.
    assert out["ZQZA"].rel_strength_benchmark == "SPY"


def test_null_adjusted_bar_anywhere_in_the_window_skips_the_symbol(db_conn: Connection) -> None:
    """The M19 skip rule, re-asserted here because this module has its own
    query and can regress independently of technicals.py."""
    last = _seed_bars(db_conn, "ZQZA", READ_DEPTH - 1)
    # The one bad bar must precede the good window, not restart at the same
    # day-zero _seed_bars defaults to — otherwise it collides on
    # (symbol, session_date) with the first good bar and the INSERT fails.
    _seed_bars(db_conn, "ZQZA", 1, adjusted=False, end=_EPOCH - timedelta(days=1))
    out = compute_and_store_standing(db_conn, USER, ["ZQZA"], last)
    assert "ZQZA" not in out


def test_symbol_with_no_bar_on_the_session_is_absent(db_conn: Connection) -> None:
    last = _seed_bars(db_conn, "ZQZA", READ_DEPTH)
    out = compute_and_store_standing(db_conn, USER, ["ZQZA"], last + timedelta(days=3))
    assert "ZQZA" not in out


# `holdings` has a UNIQUE(user_id, symbol) constraint, and the real dev tenant
# (USER above) already owns ASTS for real in this shared database — its
# `sectors`/`holdings` rows are actual dev data, not test fixtures. Any test
# below that inserts a holdings row must use a symbol the dev tenant doesn't
# already hold, so it uses "ZQZT" rather than "ASTS".
_HOLDING_SYMBOL = "ZQZT"


def test_sector_benchmark_is_used_and_named(db_conn: Connection) -> None:
    last = _seed_bars(db_conn, _HOLDING_SYMBOL, READ_DEPTH)
    _seed_bars(db_conn, "XLC", READ_DEPTH, start="80")
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    db_conn.execute(
        text("INSERT INTO sectors (id, user_id, name, benchmark_symbol, sort_order) "
             "VALUES (:i, :u, 'Comms', 'XLC', 1)"),
        {"i": "11111111-1111-1111-1111-111111111111", "u": USER},
    )
    db_conn.execute(
        text("INSERT INTO holdings (id, user_id, sector_id, symbol, status) "
             "VALUES (:i, :u, :s, :sym, 'owned')"),
        {"i": "22222222-2222-2222-2222-222222222222", "u": USER,
         "s": "11111111-1111-1111-1111-111111111111", "sym": _HOLDING_SYMBOL},
    )
    out = compute_and_store_standing(db_conn, USER, [_HOLDING_SYMBOL], last)
    assert out[_HOLDING_SYMBOL].rel_strength_benchmark == "XLC"


def test_sector_without_a_benchmark_falls_back_to_spy_and_says_so(db_conn: Connection) -> None:
    # No holdings row at all, unlike the real "ASTS" holding the dev tenant
    # actually owns (in the "space" sector, benchmarked to RKLB) — this test
    # is specifically about a symbol with no sector benchmark on record.
    last = _seed_bars(db_conn, "ZQZV", READ_DEPTH)
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    out = compute_and_store_standing(db_conn, USER, ["ZQZV"], last)
    assert out["ZQZV"].rel_strength_benchmark == "SPY"


def test_incomplete_benchmark_window_nulls_both_relative_fields(db_conn: Connection) -> None:
    """Specifically NOT a silent fallback to SPY: falling back would change the
    claim the number makes without saying so."""
    last = _seed_bars(db_conn, _HOLDING_SYMBOL, READ_DEPTH)
    _seed_bars(db_conn, "XLC", 30, start="80")
    _seed_bars(db_conn, "SPY", READ_DEPTH, start="400")
    db_conn.execute(
        text("INSERT INTO sectors (id, user_id, name, benchmark_symbol, sort_order) "
             "VALUES (:i, :u, 'Comms', 'XLC', 1)"),
        {"i": "11111111-1111-1111-1111-111111111111", "u": USER},
    )
    db_conn.execute(
        text("INSERT INTO holdings (id, user_id, sector_id, symbol, status) "
             "VALUES (:i, :u, :s, :sym, 'owned')"),
        {"i": "22222222-2222-2222-2222-222222222222", "u": USER,
         "s": "11111111-1111-1111-1111-111111111111", "sym": _HOLDING_SYMBOL},
    )
    out = compute_and_store_standing(db_conn, USER, [_HOLDING_SYMBOL], last)
    assert out[_HOLDING_SYMBOL].rel_strength is None
    assert out[_HOLDING_SYMBOL].rel_strength_benchmark is None


def test_numeric_metrics_are_stored_and_enums_are_not(db_conn: Connection) -> None:
    # No SPY seed: this test is about which metric names land in `metrics`,
    # none of which depend on benchmark resolution — a SPY window here would
    # be dead weight that implies a benchmark check this test doesn't make.
    last = _seed_bars(db_conn, "ZQZA", READ_DEPTH)
    compute_and_store_standing(db_conn, USER, ["ZQZA"], last)
    stored = {
        r[0] for r in db_conn.execute(
            text("SELECT metric FROM metrics WHERE user_id = :u AND symbol = 'ZQZA' "
                 "AND session_date = :d"),
            {"u": USER, "d": last},
        )
    }
    assert {"rsi14", "macd_hist", "adx14", "atr_pct", "rsi14_pctile"} <= stored
    assert "divergence" not in stored
    assert "rel_strength_benchmark" not in stored


def test_recomputation_is_idempotent(db_conn: Connection) -> None:
    # No SPY seed, same reasoning as above: idempotency is about the upsert's
    # ON CONFLICT behavior for `rsi14`, unaffected by benchmark resolution.
    last = _seed_bars(db_conn, "ZQZA", READ_DEPTH)
    compute_and_store_standing(db_conn, USER, ["ZQZA"], last)
    compute_and_store_standing(db_conn, USER, ["ZQZA"], last)
    n = db_conn.execute(
        text("SELECT count(*) FROM metrics WHERE user_id = :u AND symbol = 'ZQZA' "
             "AND session_date = :d AND metric = 'rsi14'"),
        {"u": USER, "d": last},
    ).scalar_one()
    assert n == 1


def test_empty_symbol_list_is_a_no_op(db_conn: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserts the `if not symbols: return {}` guard itself, not just its
    output — a `for symbol in []: ...` loop over an empty list also produces
    `{}` with no guard at all, so returning `{}` alone doesn't pin the guard
    down. Observing that no query reaches the connection is what does: without
    the guard, `_benchmarks` and `_read_windows` still each issue one query
    even for an empty symbol list."""
    spy = MagicMock(wraps=db_conn.execute)
    monkeypatch.setattr(db_conn, "execute", spy)
    assert compute_and_store_standing(db_conn, USER, [], date(2026, 6, 1)) == {}
    spy.assert_not_called()
