"""M4 against a real database (skipped without DATABASE_URL): assemble computes,
builds the object, and upserts it into ``briefs`` — idempotently on
(user_id, session_date, kind)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.assemble import SCHEMA_VERSION, assemble_and_store
from worker.constants import ATTRIBUTION_MODEL_VERSION

# A throwaway user, distinct from the real dev tenant.
_TEST_USER_ID = "00000000-0000-0000-0000-0000000000fd"

_PREV = date(2026, 8, 10)
_SESSION = date(2026, 8, 11)
_SECTOR = "ZZASSEM"


def _seed_user(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'test-assemble@example.invalid')"),
        {"u": _TEST_USER_ID},
    )


def _seed_bar(conn: Connection, symbol: str, session_date: date, close: str) -> None:
    conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"
        ),
        {"s": symbol, "d": session_date, "c": Decimal(close)},
    )


def _seed_lot(conn: Connection, symbol: str, shares: str, cost_cents: int) -> None:
    sector_id = conn.execute(
        text(
            "INSERT INTO sectors (user_id, name) VALUES (:u, :n) "
            "ON CONFLICT (user_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"u": _TEST_USER_ID, "n": _SECTOR},
    ).scalar_one()
    holding_id = conn.execute(
        text(
            "INSERT INTO holdings (user_id, sector_id, symbol) VALUES (:u, :s, :sym) RETURNING id"
        ),
        {"u": _TEST_USER_ID, "s": sector_id, "sym": symbol},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO lots (user_id, holding_id, shares, cost_basis_cents, opened_on) "
            "VALUES (:u, :h, :sh, :cb, :o)"
        ),
        {"u": _TEST_USER_ID, "h": holding_id, "sh": Decimal(shares), "cb": cost_cents, "o": _PREV},
    )


def _seed_book(conn: Connection) -> None:
    # Junk symbols only: bars_daily is shared (no user_id), so seeding a real
    # benchmark like SPY would collide with backfilled data. vs_spy is exercised
    # by the pure test; here we care about persistence and idempotency.
    _seed_user(conn)
    for symbol, prev_c, c in (("ZZA", "100", "110"), ("ZZB", "50", "48")):
        _seed_bar(conn, symbol, _PREV, prev_c)
        _seed_bar(conn, symbol, _SESSION, c)
    _seed_lot(conn, "ZZA", "10", 9000)  # cost $90
    _seed_lot(conn, "ZZB", "20", 4000)  # cost $40


def _seed_attribution(
    conn: Connection, symbol: str, *, resid_bps: str, resid_z: str, provisional: bool = False
) -> None:
    conn.execute(
        text(
            "INSERT INTO attribution "
            "(symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps, "
            " resid_z, beta_market, beta_theme, r2, n_obs, provisional, cold_start, synthetic, "
            " revised, computed_at) "
            "VALUES (:sym, :d, :mv, 0, 0, :resid_bps, :resid_bps, :resid_z, 1, 0, 0.5, 60, "
            " :provisional, false, false, false, :now)"
        ),
        {
            "sym": symbol, "d": _SESSION, "mv": ATTRIBUTION_MODEL_VERSION,
            "resid_bps": Decimal(resid_bps), "resid_z": Decimal(resid_z),
            "provisional": provisional, "now": datetime.now(UTC),
        },
    )


def _brief_rows(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT kind, schema_version, body FROM briefs "
            "WHERE user_id = :u AND session_date = :d AND kind = 'close'"
        ),
        {"u": _TEST_USER_ID, "d": _SESSION},
    ).mappings().all()
    return [dict(r) for r in rows]


def test_assemble_and_store_persists_the_object(db_conn: Connection) -> None:
    _seed_book(db_conn)

    obj = assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")
    assert obj is not None  # both names are full-tier movers → the brief sends

    rows = _brief_rows(db_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == SCHEMA_VERSION
    # The stored jsonb is exactly the object we returned.
    assert row["body"] == obj.model_dump(mode="json")
    # Close prices made it into the attribution rows.
    body = row["body"]
    rows_by_symbol = {r["symbol"]: r for r in body["sections"][0]["rows"]}
    assert rows_by_symbol["ZZA"]["close"] == 110.0
    assert body["book"]["day_bps"] == 300


def test_attribution_rows_ranked_by_resid_z_end_to_end(db_conn: Connection) -> None:
    # ZZC barely moves on the tape but has the largest |resid_z| (a
    # residual-material name, M13 §2) → forced full tier and ranks first.
    # ZZA is a big raw mover with a small residual; ZZB likewise, smaller still.
    _seed_user(db_conn)
    for symbol, prev_c, c in (
        ("ZZA", "100", "110"),   # +10.0% raw move, small residual
        ("ZZB", "50", "48"),     # -4.0% raw move, smaller residual
        ("ZZC", "100", "100.05"),  # +0.05% raw move, large residual
    ):
        _seed_bar(db_conn, symbol, _PREV, prev_c)
        _seed_bar(db_conn, symbol, _SESSION, c)
    _seed_lot(db_conn, "ZZA", "10", 9000)
    _seed_lot(db_conn, "ZZB", "20", 4000)
    _seed_lot(db_conn, "ZZC", "5", 10000)

    _seed_attribution(db_conn, "ZZA", resid_bps="30", resid_z="0.3")
    _seed_attribution(db_conn, "ZZB", resid_bps="20", resid_z="-0.2")
    _seed_attribution(db_conn, "ZZC", resid_bps="400", resid_z="4.0", provisional=True)

    obj = assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")
    assert obj is not None

    body = _brief_rows(db_conn)[0]["body"]
    rows = body["sections"][0]["rows"]
    symbols_in_order = [r["symbol"] for r in rows]

    # (a) the small-raw-move, large-|resid_z| name is first.
    assert symbols_in_order[0] == "ZZC"
    # (c) the large-raw-move, small-|resid_z| name is not first.
    assert symbols_in_order[0] != "ZZA"

    # (b) rows carry resid_bps / resid_z / provisional.
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["ZZC"]["resid_bps"] == 400
    assert by_symbol["ZZC"]["resid_z"] == 4.0
    assert by_symbol["ZZC"]["provisional"] is True
    assert by_symbol["ZZC"]["tier"] == "full"  # forced full despite the flat move
    assert by_symbol["ZZA"]["resid_bps"] == 30
    assert by_symbol["ZZA"]["resid_z"] == 0.3


def test_reassembly_upserts_not_duplicates(db_conn: Connection) -> None:
    _seed_book(db_conn)

    assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")
    assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")

    assert len(_brief_rows(db_conn)) == 1


def test_quiet_session_writes_nothing(db_conn: Connection) -> None:
    # Both names move <0.3% → suppressed, no full-tier mover → skip, no row.
    _seed_user(db_conn)
    for symbol, prev_c, c in (("ZZA", "100", "100.1"), ("ZZB", "50", "49.95")):
        _seed_bar(db_conn, symbol, _PREV, prev_c)
        _seed_bar(db_conn, symbol, _SESSION, c)
    _seed_lot(db_conn, "ZZA", "10", 9000)
    _seed_lot(db_conn, "ZZB", "20", 4000)

    obj = assemble_and_store(db_conn, _TEST_USER_ID, _SESSION, "close")

    assert obj is None
    assert _brief_rows(db_conn) == []


def test_tape_metrics_are_persisted(db_conn: Connection) -> None:
    # 30 prior sessions of flat volume, then a session with a real intraday range
    # and double volume → rvol and range_position land in `metrics`.
    _seed_user(db_conn)
    from datetime import timedelta

    day = _SESSION - timedelta(days=40)
    for _ in range(30):
        db_conn.execute(
            text(
                "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                "VALUES ('ZZT', :d, 100, 100, 100, 100, 1000, 100)"
            ),
            {"d": day},
        )
        day += timedelta(days=1)
    # session bar: close in the top quarter of a 96→104 range, volume 2000
    db_conn.execute(
        text(
            "INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
            "VALUES ('ZZT', :d, 100, 104, 96, 102, 2000, 102)"
        ),
        {"d": _SESSION},
    )

    from worker.tape import compute_and_store_tape

    tape = compute_and_store_tape(db_conn, _TEST_USER_ID, ["ZZT"], _SESSION)
    rvol, rp = tape["ZZT"].rvol, tape["ZZT"].range_position
    assert rvol is not None and float(rvol) == 2.0  # 2000 / mean(1000 × 30)
    assert rp is not None and float(rp) == 0.75  # (102-96)/(104-96)

    stored = {
        r["metric"]: r["value"]
        for r in db_conn.execute(
            text(
                "SELECT metric, value FROM metrics WHERE user_id = :u "
                "AND symbol = 'ZZT' AND session_date = :d"
            ),
            {"u": _TEST_USER_ID, "d": _SESSION},
        ).mappings()
    }
    assert float(stored["rvol"]) == 2.0
    assert float(stored["range_position"]) == 0.75
