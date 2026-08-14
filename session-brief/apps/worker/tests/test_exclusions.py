from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker.exclusions import contaminated_days


def test_earnings_day_is_flagged_index_empty_is_noop(db_conn: Connection) -> None:
    db_conn.execute(text(
        "INSERT INTO events (symbol, event_type, occurs_at) "
        "VALUES ('ZZZ', 'earnings', :d)"
    ), {"d": date(2020, 3, 2)})
    mask = contaminated_days(db_conn, ["ZZZ"], date(2020, 1, 1), date(2020, 6, 30))
    assert mask.get("ZZZ") == {date(2020, 3, 2)}
    # A symbol with no events and empty index_events contributes nothing.
    assert contaminated_days(db_conn, ["QQQ"], date(2020, 1, 1), date(2020, 6, 30)) == {}
