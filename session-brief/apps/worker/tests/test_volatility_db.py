"""M22 DB layer: the volatility reading over seeded bars, VIX history and two
sessions of synthetic option snapshots."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.calendar import previous_session
from worker.options_ingest import pull_options
from worker.providers.cboe import parse_chain
from worker.providers.synthetic import SyntheticOptionsProvider
from worker.volatility import (
    VolReading,
    compute_and_store_volatility,
    read_premium_changes,
    read_vix_pctile,
    select_strip,
)

# A dedicated throwaway user, distinct from the real dev tenant.
USER = "00000000-0000-0000-0000-0000000000f7"

# Late in exchange_calendars' window (it ends a year out) and clear of any real
# feed, as in test_options_ingest_db.py.
SESSIONS = (date(2027, 9, 14), date(2027, 9, 15))
FRESH = ["SPY", "ZZTHN", "ZZVOL"]  # pulled on both sessions
STALE = "ZZSTL"  # pulled on the first session only
OFFSET = "ZZOFF"  # pulled on both; its bars print a dollar above the payload close
NO_BARS = "ZZNOB"  # pulled on both; no bars at all
N_BARS = 300
N_VIX = 260

METRIC_KEYS = (
    "iv30",
    "iv30_n",
    "iv30_cboe",
    "iv30_pctile",
    "iv_rank_252",
    "hv20",
    "hv60",
    "iv_hv_spread",
    "iv_hv_ratio",
    "iv_vix_ratio",
    "iv_vix_ratio_pctile",
    "vix_pctile",
    "iv_term_slope",
    "contracts_writable",
)


def _sessions_back(last: date, n: int) -> list[date]:
    days = [last]
    while len(days) < n:
        days.append(previous_session(days[-1]))
    return days[::-1]


def _close(i: int) -> Decimal:
    # Multiples of 0.40, so every synthetic strike (2.5% steps) lands on a cent
    # and the chain's IVs come out exact.
    return Decimal("100") + Decimal("0.40") * Decimal((i * 7) % 11 - 5)


def _seed(conn: Connection) -> dict[date, Decimal]:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-volatility@example.invalid')"),
        {"u": USER},
    )
    days = _sessions_back(SESSIONS[1], N_BARS)
    closes = {d: _close(i) for i, d in enumerate(days)}
    # Same close on both option sessions, so the strip lands on the same
    # contracts and the same-contract change has a prior to read.
    closes[SESSIONS[0]] = closes[SESSIONS[1]]
    # ON CONFLICT: SPY may already carry real bars on the older dates.
    conn.execute(
        text("""
            INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v,
                                    adj_c, adj_o, adj_h, adj_l, adj_v)
            VALUES (:s, :d, :c, :c, :c, :c, 1000000, :c, :c, :c, :c, 1000000)
            ON CONFLICT DO NOTHING
        """),
        [
            *({"s": s, "d": d, "c": c} for s in [*FRESH, STALE] for d, c in closes.items()),
            *({"s": OFFSET, "d": d, "c": c + 1} for d, c in closes.items()),
        ],
    )
    vix_days = _sessions_back(previous_session(SESSIONS[0]), N_VIX)
    conn.execute(
        text("""
            INSERT INTO index_daily (symbol, session_date, c) VALUES ('VIX', :d, :c)
            ON CONFLICT DO NOTHING
        """),
        [{"d": d, "c": Decimal(15 + i % 11)} for i, d in enumerate(vix_days)],
    )
    return closes


def _pull(conn: Connection, closes: dict[date, Decimal]) -> None:
    both = [*FRESH, OFFSET, NO_BARS]
    for session, symbols in ((SESSIONS[0], [*both, STALE]), (SESSIONS[1], both)):
        provider = SyntheticOptionsProvider({s: closes[session] for s in symbols}, session)
        result = pull_options(conn, provider, symbols, session)
        assert result.symbols_failed == {}
    # ZZTHN's selected snapshots all go one-sided: no bid anywhere.
    conn.execute(text("UPDATE option_snapshot SET bid = 0 WHERE contract LIKE 'ZZTHN%'"))


def _own(conn: Connection, symbol: str, shares: str, *, closed_on: date | None = None) -> None:
    sector_id = conn.execute(
        text(
            "INSERT INTO sectors (user_id, name) VALUES (:u, 'vol-test') "
            "ON CONFLICT (user_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"u": USER},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": USER, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text("""
            INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on, closed_on)
            VALUES (:u, :h, :sh, 1000000, :o, :c)
        """),
        {
            "u": USER,
            "h": holding_id,
            "sh": Decimal(shares),
            "o": SESSIONS[0] - timedelta(days=30),
            "c": closed_on,
        },
    )


def _run(conn: Connection) -> tuple[dict[str, VolReading], dict[str, VolReading]]:
    symbols = [*FRESH, STALE]
    first = compute_and_store_volatility(conn, USER, symbols, SESSIONS[0])
    second = compute_and_store_volatility(conn, USER, symbols, SESSIONS[1])
    return first, second


def test_second_session_reading_and_its_stored_rows(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    db_conn.execute(
        text("""
            INSERT INTO events (symbol, event_type, occurs_at)
            VALUES ('ZZVOL', 'earnings', :d)
        """),
        {"d": SESSIONS[1] + timedelta(days=10)},
    )
    first, second = _run(db_conn)
    assert first["ZZVOL"].strip[0].chg_tenor_pct is None  # no prior strip yet

    r = second["ZZVOL"]
    assert r.iv30 is not None and abs(r.iv30 - 30) <= 1
    assert r.iv30_cboe == Decimal("30.0")
    assert not r.iv30_thin and not r.stale
    assert r.hv20 is not None and r.hv60 is not None
    assert r.iv_hv_spread == r.iv30 - r.hv20
    assert r.vix_close is not None and r.iv_vix_ratio == r.iv30 / r.vix_close
    assert r.vix_pctile is not None and 0 <= r.vix_pctile <= 100
    assert r.iv30_pctile is None  # one prior session, not 252
    assert r.iv_rank_252 is None and r.iv_vix_ratio_pctile is None
    assert len(r.strip) == 12
    assert r.strip[0].chg_tenor_pct is not None
    assert r.strip[0].chg_contract_pct is not None
    assert r.earnings_date == SESSIONS[1] + timedelta(days=10)
    assert r.contracts_writable == 0  # no lots
    assert r.premium in {"rich", "fair", "cheap"}

    stored = {
        str(m): Decimal(v)
        for m, v in db_conn.execute(
            text("""
                SELECT metric, value FROM metrics
                WHERE user_id = :u AND symbol = 'ZZVOL' AND session_date = :d
            """),
            {"u": USER, "d": SESSIONS[1]},
        ).all()
    }
    expected = {k for k in METRIC_KEYS if getattr(r, k) is not None}
    assert set(stored) == expected
    assert stored["iv30_n"] == r.iv30_n
    assert stored["iv30"] == r.iv30.quantize(Decimal("0.0000000001"))

    counts = db_conn.execute(
        text("""
            SELECT session_date, count(*) FROM option_strip
            WHERE symbol = 'ZZVOL' GROUP BY 1 ORDER BY 1
        """)
    ).all()
    assert [tuple(c) for c in counts] == [(SESSIONS[0], 12), (SESSIONS[1], 12)]


def test_earnings_outside_the_window_is_not_carried(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    db_conn.execute(
        text("""
            INSERT INTO events (symbol, event_type, occurs_at)
            VALUES ('ZZVOL', 'earnings', :d)
        """),
        {"d": SESSIONS[1] + timedelta(days=90)},
    )
    _, second = _run(db_conn)
    assert second["ZZVOL"].earnings_date is None


def test_contracts_writable_counts_open_lots_only(db_conn: Connection) -> None:
    """Review Focus 5: a lot closed on session 1 is not writable on session 2."""
    _pull(db_conn, _seed(db_conn))
    _own(db_conn, "ZZVOL", "250")
    _own(db_conn, "SPY", "250", closed_on=SESSIONS[0])
    _, second = _run(db_conn)
    assert second["ZZVOL"].contracts_writable == 2
    assert second["SPY"].contracts_writable == 0


def test_a_symbol_missing_todays_snapshot_reads_yesterdays_as_stale(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    first, second = _run(db_conn)
    assert not first[STALE].stale
    stale = second[STALE]
    assert stale.stale
    assert stale.iv30 is not None
    assert not second["ZZVOL"].stale


def test_a_symbol_with_no_snapshot_either_day_is_omitted(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    out = compute_and_store_volatility(db_conn, USER, ["ZZVOL", "ZZNONE"], SESSIONS[1])
    assert set(out) == {"ZZVOL"}


def test_a_one_sided_chain_is_thin_and_fair(db_conn: Connection) -> None:
    """Review Focus 2: no two-sided quote anywhere yields no IV30 at all."""
    _pull(db_conn, _seed(db_conn))
    _, second = _run(db_conn)
    r = second["ZZTHN"]
    assert r.iv30 is None
    assert r.iv30_n == 0
    assert r.iv30_thin
    assert r.premium == "fair"
    assert all(row.contract is None for row in r.strip)


def test_selection_measures_against_the_stored_payload_close(db_conn: Connection) -> None:
    """Ingest selected against the payload's close, not the bar's: re-reading
    must land on the very contracts ingest chose."""
    closes = _seed(db_conn)
    _pull(db_conn, closes)
    out = compute_and_store_volatility(db_conn, USER, [OFFSET], SESSIONS[1])
    r = out[OFFSET]
    payload_close = closes[SESSIONS[1]]
    assert r.close == payload_close
    assert r.iv30_cboe == Decimal("30.0")
    full = SyntheticOptionsProvider({OFFSET: payload_close}, SESSIONS[1]).chain(OFFSET)
    _, contracts = parse_chain(full)
    expected = [
        c.contract.symbol if c.contract else None
        for c in select_strip(contracts, payload_close, SESSIONS[1])
    ]
    assert [row.contract for row in r.strip] == expected
    assert all(row.contract is not None for row in r.strip)
    atm = next(row for row in r.strip if (row.tenor, row.rule) == (30, "atm"))
    assert atm.moneyness == 0


def test_selection_falls_back_to_the_bar_close_without_the_payload(db_conn: Connection) -> None:
    closes = _seed(db_conn)
    _pull(db_conn, closes)
    db_conn.execute(
        text("""
            DELETE FROM raw_payloads
            WHERE source = 'cboe' AND endpoint = 'delayed_quotes/options'
              AND symbol = :s AND as_of = :d
        """),
        {"s": OFFSET, "d": SESSIONS[1]},
    )
    r = compute_and_store_volatility(db_conn, USER, [OFFSET], SESSIONS[1])[OFFSET]
    bar_close = closes[SESSIONS[1]] + 1
    assert r.close == bar_close
    assert r.iv30_cboe is None
    atm = next(row for row in r.strip if (row.tenor, row.rule) == (30, "atm"))
    assert atm.moneyness == closes[SESSIONS[1]] / bar_close - 1


def test_a_symbol_with_snapshots_but_no_bars_keeps_its_reading(db_conn: Connection) -> None:
    closes = _seed(db_conn)
    _pull(db_conn, closes)
    r = compute_and_store_volatility(db_conn, USER, [NO_BARS], SESSIONS[1])[NO_BARS]
    assert r.iv30 is not None
    assert r.close == closes[SESSIONS[1]]
    assert r.hv20 is None and r.hv60 is None
    assert r.iv_hv_spread is None and r.iv_hv_ratio is None
    assert r.premium == "fair"
    assert all(row.contract is not None for row in r.strip)


# --- Regime changes since the prior close (M25) -----------------------------

# The open session; the two closes it reads are the sessions before it.
OPEN_SESSION = date(2027, 9, 16)


def _seed_metrics(conn: Connection, session_date: date, rows: dict[str, dict[str, str]]) -> None:
    conn.execute(
        text("""
            INSERT INTO metrics (user_id, symbol, session_date, metric, value)
            VALUES (:u, :s, :d, :m, :v)
        """),
        [
            {"u": USER, "s": symbol, "d": session_date, "m": metric, "v": Decimal(value)}
            for symbol, values in rows.items()
            for metric, value in values.items()
        ],
    )


def test_read_premium_changes_names_the_flipped_regime(db_conn: Connection) -> None:
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-volatility@example.invalid')"),
        {"u": USER},
    )
    last = previous_session(OPEN_SESSION)
    before = previous_session(last)
    steady = {"iv30_n": "8", "iv30_pctile": "50", "iv_hv_spread": "0.02", "vix_pctile": "41"}
    # VIX is one figure per session, stored on every held name's row.
    today = {**steady, "vix_pctile": "63"}
    # ZZFLP: fair (mid-year) the session before, rich (top of its year) at the
    # last close. ZZTHN: thin at the last close, so its rich numbers read fair.
    _seed_metrics(db_conn, before, {"ZZFLP": steady, "ZZSTD": steady, "ZZTHN": steady})
    _seed_metrics(
        db_conn,
        last,
        {
            "ZZFLP": {**today, "iv30_pctile": "85"},
            "ZZSTD": today,
            "ZZTHN": {**today, "iv30_pctile": "90", "iv30_n": "2"},
        },
    )

    symbols = ["ZZFLP", "ZZSTD", "ZZTHN"]
    assert read_premium_changes(db_conn, USER, symbols, OPEN_SESSION) == [("ZZFLP", "fair", "rich")]
    assert read_vix_pctile(db_conn, USER, symbols, OPEN_SESSION) == Decimal("63")
    assert read_vix_pctile(db_conn, USER, symbols, last) == Decimal("41")
