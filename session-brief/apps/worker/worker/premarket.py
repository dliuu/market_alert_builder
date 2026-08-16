"""The pre-market feed: vendor shape → `quotes` → the open brief's §2/§3 (M15).

Kept out of `assemble_open.py` for the reason `tape.py` is kept out of
`compute.py`: the assembler should read a value, not know how it was measured.

Two rules earn their own module here:

- **The volume multiple is pre-market-specific.** It compares this morning's
  pre-market volume with the same measure on prior mornings, never with the
  30-day daily RVOL. Pre-market volume is a different, far thinner series, and a
  daily-volume ratio over it is a number that looks meaningful and isn't (D3,
  docs/05).
- **The gap is dollars, per share.** A percent tells you how the stock moved;
  cents tell you what a share of it did, which is the figure you act on
  (docs/01). Per-share, not per-position — the open brief carries no position
  data by design (no lots, no shares held), so there is no P&L figure here to
  compute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker import config
from worker.constants import PREMARKET_THRESHOLD, TAPE_SYMBOLS
from worker.providers.base import PremarketProvider


@dataclass(frozen=True)
class PremarketQuote:
    """One held name's pre-open state. ``typical_v`` is the mean pre-market
    volume over the prior sessions in the window, or None when there isn't
    enough history to say what typical means."""

    symbol: str
    extended_last: Decimal
    extended_v: int
    prev_close: Decimal
    typical_v: Decimal | None


@dataclass(frozen=True)
class TapeQuote:
    """One §2 row: a macro or foreign-proxy series, overnight."""

    symbol: str
    label: str
    last: Decimal
    prev_close: Decimal


# --- Pure math ------------------------------------------------------------


def pre_pct(quote: PremarketQuote) -> Decimal | None:
    if quote.prev_close == 0:
        return None
    return quote.extended_last / quote.prev_close - 1


def gap_cents(quote: PremarketQuote) -> int:
    """The gap in integer cents (money invariant), rounded as a broker rounds."""
    return int(
        ((quote.extended_last - quote.prev_close) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def premarket_vol_mult(quote: PremarketQuote) -> Decimal | None:
    if quote.typical_v is None or quote.typical_v == 0:
        return None
    return Decimal(quote.extended_v) / quote.typical_v


def clears_threshold(quote: PremarketQuote, *, has_news: bool = False) -> bool:
    """docs/05 §3: only names moving more than 1% pre-market, or carrying news.

    ``has_news`` is wired by the caller (`assemble_open._premarket`) from FDN's
    `latest-news` when `FDN_API_KEY` is set (M16); with no key it is always
    `False`, the same threshold-without-a-source shape `short_interest` still
    has per D18.
    """
    if has_news:
        return True
    pct = pre_pct(quote)
    return pct is not None and abs(pct) > PREMARKET_THRESHOLD


def tape_change(quote: TapeQuote) -> tuple[Decimal | None, Decimal]:
    """(overnight fraction, overnight absolute). The fraction is None for
    level-quoted series — see `constants.LEVEL_QUOTED`."""
    from worker.constants import LEVEL_QUOTED

    absolute = quote.last - quote.prev_close
    if quote.symbol in LEVEL_QUOTED or quote.prev_close == 0:
        return None, absolute
    return quote.last / quote.prev_close - 1, absolute


def capture_stamp(session_date: date) -> datetime:
    """The pre-open capture instant, in UTC (invariant 8: UTC at rest,
    America/New_York in logic)."""
    from datetime import time as clock_time
    from zoneinfo import ZoneInfo

    from worker import calendar

    et = clock_time(config.PREMARKET_CAPTURE_ET_HOUR, config.PREMARKET_CAPTURE_ET_MINUTE)
    return datetime.combine(session_date, et, tzinfo=calendar.ET).astimezone(ZoneInfo("UTC"))


def tape_universe(sector_benchmarks: list[str]) -> list[tuple[str, str, str]]:
    """The §2 symbol list: the fixed macro tape plus one foreign proxy per
    mapped sector benchmark in *this* book. A book with no semis sleeve gets no
    Taiwan line — that is what makes §2 relevant rather than generic."""
    from worker.constants import FOREIGN_PROXIES

    out = list(TAPE_SYMBOLS)
    seen = {symbol for symbol, _, _ in out}
    for benchmark in sorted(set(sector_benchmarks)):
        proxy = FOREIGN_PROXIES.get(benchmark)
        if proxy is not None and proxy[0] not in seen:
            out.append((proxy[0], proxy[1], "index"))
            seen.add(proxy[0])
    return out


# --- Database layer -------------------------------------------------------

# Two column-scoped upserts, not one (I3, M15 review): a symbol that shows up
# in both the held list and the tape list (a holding whose ticker is also a
# sector's foreign proxy, e.g. holding EWT while running a semis sleeve
# benchmarked to SMH) must have both writes compose rather than clobber. Each
# statement touches only its own feed's value columns plus `captured_at`, so
# the second write can never blank the first's columns back to NULL.
_UPSERT_HELD = text("""
    INSERT INTO quotes (symbol, session_date, captured_at, prev_close,
                        extended_last, extended_v)
    VALUES (:symbol, :session_date, :captured_at, :prev_close,
            :extended_last, :extended_v)
    ON CONFLICT (symbol, session_date) DO UPDATE
        SET captured_at = EXCLUDED.captured_at,
            prev_close = EXCLUDED.prev_close,
            extended_last = EXCLUDED.extended_last,
            extended_v = EXCLUDED.extended_v
""")

_UPSERT_TAPE = text("""
    INSERT INTO quotes (symbol, session_date, captured_at, last, prev_close)
    VALUES (:symbol, :session_date, :captured_at, :last, :prev_close)
    ON CONFLICT (symbol, session_date) DO UPDATE
        SET captured_at = EXCLUDED.captured_at,
            last = EXCLUDED.last,
            prev_close = EXCLUDED.prev_close
""")

_READ_PRIOR_CLOSES = text("""
    SELECT symbol, c FROM bars_daily
    WHERE session_date = :prior_session AND symbol = ANY(:symbols)
""")

_READ_PREMARKET = text("""
    SELECT symbol, extended_last, extended_v, prev_close FROM quotes
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
      AND extended_last IS NOT NULL AND prev_close IS NOT NULL
    ORDER BY symbol
""")

# The typical-pre-market-volume base: the prior sessions' captures for this
# symbol, most recent first. Today is excluded — the same discipline tape.py
# applies to RVOL.
_READ_PRIOR_VOLUMES = text("""
    SELECT extended_v FROM quotes
    WHERE symbol = :symbol AND session_date < :session_date
      AND extended_v IS NOT NULL
    ORDER BY session_date DESC
    LIMIT :window
""")

_READ_TAPE = text("""
    SELECT symbol, last, prev_close FROM quotes
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
      AND last IS NOT NULL AND prev_close IS NOT NULL
""")


def prior_closes(
    conn: Connection, symbols: list[str], prior_session: date
) -> dict[str, Decimal]:
    """The bases every pre-market figure is measured from."""
    return {
        str(row["symbol"]): Decimal(str(row["c"]))
        for row in conn.execute(
            _READ_PRIOR_CLOSES, {"prior_session": prior_session, "symbols": symbols}
        ).mappings()
    }


def ingest_premarket(
    conn: Connection,
    provider: PremarketProvider,
    *,
    held: list[str],
    tape: list[tuple[str, str, str]],
    session_date: date,
    captured_at: datetime,
) -> int:
    """Fetch the morning's quotes and write them to `quotes`. Returns rows
    written.

    This is the only writer, and it is provider-agnostic: the synthetic seed and
    a licensed fdnpy feed take exactly this path, which is what makes the swap a
    constructor change rather than a section rewrite.
    """
    written = 0
    for record in provider.get_latest_prices(held):
        conn.execute(_UPSERT_HELD, {
            "symbol": record["symbol"],
            "session_date": session_date,
            "captured_at": captured_at,
            "prev_close": record["prev_close"],
            "extended_last": record["extended_last"],
            "extended_v": record["extended_v"],
        })
        written += 1

    by_feed: dict[str, list[str]] = {}
    for symbol, _label, feed in tape:
        by_feed.setdefault(feed, []).append(symbol)
    fetch = {
        "futures": provider.get_futures_prices,
        "index": provider.get_index_quotes,
        "forex": provider.get_forex_quotes,
    }
    for feed, symbols in by_feed.items():
        for record in fetch[feed](symbols):
            conn.execute(_UPSERT_TAPE, {
                "symbol": record["symbol"],
                "session_date": session_date,
                "captured_at": captured_at,
                "last": record["last"],
                "prev_close": record["prev_close"],
            })
            written += 1
    return written


def read_premarket(
    conn: Connection, symbols: list[str], session_date: date
) -> list[PremarketQuote]:
    """§3's inputs, with each name's typical pre-market volume attached."""
    out: list[PremarketQuote] = []
    for row in conn.execute(
        _READ_PREMARKET, {"session_date": session_date, "symbols": symbols}
    ).mappings():
        out.append(
            PremarketQuote(
                symbol=str(row["symbol"]),
                extended_last=Decimal(str(row["extended_last"])),
                extended_v=int(row["extended_v"] or 0),
                prev_close=Decimal(str(row["prev_close"])),
                typical_v=_typical_volume(conn, str(row["symbol"]), session_date),
            )
        )
    return out


def _typical_volume(conn: Connection, symbol: str, session_date: date) -> Decimal | None:
    volumes = [
        Decimal(str(r[0]))
        for r in conn.execute(
            _READ_PRIOR_VOLUMES,
            {"symbol": symbol, "session_date": session_date,
             "window": config.PREMARKET_VOL_WINDOW},
        ).all()
    ]
    if len(volumes) < config.PREMARKET_VOL_MIN_OBS:
        return None
    return sum(volumes, Decimal(0)) / Decimal(len(volumes))


def read_tape(
    conn: Connection, session_date: date, sector_benchmarks: list[str]
) -> list[TapeQuote]:
    """§2's rows, in the tape's declared order — the reading order of docs/05,
    not whatever the database returns."""
    universe = tape_universe(sector_benchmarks)
    labels = {symbol: label for symbol, label, _ in universe}
    stored = {
        str(row["symbol"]): row
        for row in conn.execute(
            _READ_TAPE, {"session_date": session_date, "symbols": list(labels)}
        ).mappings()
    }
    return [
        TapeQuote(
            symbol=symbol,
            label=labels[symbol],
            last=Decimal(str(stored[symbol]["last"])),
            prev_close=Decimal(str(stored[symbol]["prev_close"])),
        )
        for symbol, _label, _feed in universe
        if symbol in stored
    ]
