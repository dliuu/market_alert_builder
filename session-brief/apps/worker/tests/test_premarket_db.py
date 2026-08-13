"""The `quotes` table and the pre-market ingest path (M15), against a real
database (skipped without DATABASE_URL). Rolled back per test."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

_SESSION = date(2098, 4, 7)
_CAPTURED = datetime(2098, 4, 7, 12, 12, tzinfo=UTC)
_PRIOR = date(2098, 4, 6)


def _seed_bars(conn: Connection) -> None:
    for symbol, close in (("ZHELD", "10.00"), ("ES=F", "5600.00")):
        conn.execute(
            text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                 "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"),
            {"s": symbol, "d": _PRIOR, "c": Decimal(close)},
        )


def test_quotes_upsert_is_idempotent_per_session(db_conn: Connection) -> None:
    """One capture per symbol per session — re-running the pre-open seed
    replaces rather than duplicating (the events_seed precedent)."""
    upsert = text("""
        INSERT INTO quotes (symbol, session_date, captured_at, last, prev_close,
                            extended_last, extended_v)
        VALUES (:s, :d, :t, :last, :prev, :ext, :extv)
        ON CONFLICT (symbol, session_date) DO UPDATE
            SET captured_at = EXCLUDED.captured_at,
                last = EXCLUDED.last,
                prev_close = EXCLUDED.prev_close,
                extended_last = EXCLUDED.extended_last,
                extended_v = EXCLUDED.extended_v
    """)
    row = {
        "s": "ZQUOTE", "d": _SESSION, "t": _CAPTURED,
        "last": Decimal("10.00"), "prev": Decimal("9.50"),
        "ext": Decimal("10.25"), "extv": 12345,
    }
    db_conn.execute(upsert, row)
    db_conn.execute(upsert, {**row, "ext": Decimal("10.75")})

    stored = db_conn.execute(
        text("SELECT extended_last, extended_v FROM quotes "
             "WHERE symbol = :s AND session_date = :d"),
        {"s": "ZQUOTE", "d": _SESSION},
    ).all()
    assert len(stored) == 1
    assert Decimal(str(stored[0][0])) == Decimal("10.75")
    assert stored[0][1] == 12345


def test_quotes_carries_no_user_id(db_conn: Connection) -> None:
    """Market data is shared across the tenant base, keyed by symbol (D18/D21)."""
    columns = {
        r[0]
        for r in db_conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name = 'quotes'")
        ).all()
    }
    assert "user_id" not in columns
    assert {"symbol", "session_date", "captured_at", "last", "prev_close",
            "extended_last", "extended_v"} <= columns


def test_ingest_then_read_round_trips(db_conn: Connection) -> None:
    from worker.premarket import (
        capture_stamp,
        ingest_premarket,
        prior_closes,
        read_premarket,
        read_tape,
    )
    from worker.providers.synthetic import SyntheticPremarketProvider

    _seed_bars(db_conn)  # ZHELD + ES=F etc. at _PRIOR, see helper below
    closes = prior_closes(db_conn, ["ZHELD", "ES=F"], _PRIOR)
    provider = SyntheticPremarketProvider(closes, _SESSION)

    written = ingest_premarket(
        db_conn, provider,
        held=["ZHELD"],
        tape=[("ES=F", "ES futures", "futures")],
        session_date=_SESSION,
        captured_at=capture_stamp(_SESSION),
    )
    assert written == 2

    (quote,) = read_premarket(db_conn, ["ZHELD"], _SESSION)
    assert quote.prev_close == closes["ZHELD"]
    (tape,) = read_tape(db_conn, _SESSION, sector_benchmarks=[])
    assert tape.label == "ES futures"


def test_a_live_provider_takes_the_same_path(db_conn: Connection) -> None:
    """DoD 5: the seed and a (mock) live feed produce the same stored shape —
    the seam, proven, not asserted."""
    from decimal import Decimal

    from worker.premarket import capture_stamp, ingest_premarket, read_premarket

    class MockLive:
        """Stands in for a licensed fdnpy feed: same methods, canned rows."""

        def get_latest_prices(self, symbols: list[str]) -> list[dict[str, object]]:
            return [{"symbol": s, "extended_last": Decimal("11.00"),
                     "extended_v": 999, "prev_close": Decimal("10.00")} for s in symbols]

        def get_futures_prices(self, symbols: list[str]) -> list[dict[str, object]]:
            return []

        get_index_quotes = get_futures_prices
        get_forex_quotes = get_futures_prices

    ingest_premarket(
        db_conn, MockLive(),
        held=["ZLIVE"], tape=[], session_date=_SESSION,
        captured_at=capture_stamp(_SESSION),
    )
    (quote,) = read_premarket(db_conn, ["ZLIVE"], _SESSION)
    assert quote.extended_last == Decimal("11.00")
    assert quote.extended_v == 999


def test_typical_volume_excludes_today_and_respects_the_window(db_conn: Connection) -> None:
    """`_typical_volume` is the riskiest SQL in this module: it must exclude
    today's session, order by session_date DESC, and take only the most recent
    PREMARKET_VOL_WINDOW prior sessions. The pure tests never reach this SQL —
    they hand `PremarketQuote` a `typical_v` directly — so this seeds `quotes`
    for real and reads back through `read_premarket`.

    Seeded so any of the three ways this could silently break moves the
    answer far from the expected 100_000, not just slightly:
    - flip `<` to `<=` (today included) → today's 9_000_000 pulls the mean
      to 990_000.
    - flip `DESC` to `ASC` (wrong end of the window) → the 5_000_000 outlier
      11 sessions back is pulled in instead of the most recent session,
      pulling the mean to 590_000.
    - drop the `LIMIT` (whole history averaged) → the outlier pulls the mean
      to ~545_454.
    """
    from worker import config
    from worker.premarket import read_premarket

    symbol = "ZVOL"
    insert = text("""
        INSERT INTO quotes (symbol, session_date, captured_at, last, prev_close,
                            extended_last, extended_v)
        VALUES (:s, :d, :t, :last, :prev, :ext, :extv)
    """)

    # The PREMARKET_VOL_WINDOW most recent prior sessions — the ones that must
    # be averaged.
    for i in range(1, config.PREMARKET_VOL_WINDOW + 1):
        db_conn.execute(insert, {
            "s": symbol, "d": _SESSION - timedelta(days=i), "t": _CAPTURED,
            "last": None, "prev": None, "ext": None, "extv": 100_000,
        })

    # One session older than the window — must be excluded by the LIMIT/ORDER.
    db_conn.execute(insert, {
        "s": symbol, "d": _SESSION - timedelta(days=config.PREMARKET_VOL_WINDOW + 1),
        "t": _CAPTURED, "last": None, "prev": None, "ext": None, "extv": 5_000_000,
    })

    # Today's row — must be excluded from the volume base, though it is still
    # the row `read_premarket` itself returns.
    db_conn.execute(insert, {
        "s": symbol, "d": _SESSION, "t": _CAPTURED,
        "last": None, "prev": Decimal("10.00"), "ext": Decimal("10.50"), "extv": 9_000_000,
    })

    (quote,) = read_premarket(db_conn, [symbol], _SESSION)
    assert quote.typical_v == Decimal("100000")
