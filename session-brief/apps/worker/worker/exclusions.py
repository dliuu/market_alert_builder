"""Contaminated-day exclusion mask (M12): days excluded from the FIT sample but
still scored, because they corrupt beta while being precisely the days worth
measuring. Earnings come from `events`; index-reconstitution from `index_events`
(ships empty, curated point-in-time). Ex-div needs no exclusion — returns are
computed from adj_c, which already removes the gap (spec)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

_EARNINGS = text("""
    SELECT symbol, occurs_at AS d FROM events
    WHERE event_type = 'earnings'
      AND symbol = ANY(:syms) AND occurs_at BETWEEN :start AND :end
""")

_INDEX = text("""
    SELECT symbol, trade_date AS d FROM index_events
    WHERE symbol = ANY(:syms) AND trade_date BETWEEN :start AND :end
""")


def contaminated_days(
    conn: Connection, symbols: list[str], start: date, end: date
) -> dict[str, set[date]]:
    out: dict[str, set[date]] = {}
    params = {"syms": symbols, "start": start, "end": end}
    for sql in (_EARNINGS, _INDEX):
        for row in conn.execute(sql, params).mappings():
            out.setdefault(row["symbol"], set()).add(row["d"])
    return out
