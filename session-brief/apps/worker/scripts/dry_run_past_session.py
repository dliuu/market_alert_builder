"""Assemble a brief for a past session using real bars_daily history, without
touching the real book. Useful before your actual lots have enough trading-day
history behind them for `worker.cli brief` to work directly.

Temporarily backdates every lot's `opened_on` inside a transaction that is
always rolled back — nothing is persisted, real data is never modified.

Run as a module (from apps/worker) so the `worker` package resolves:
    uv run python -m scripts.dry_run_past_session --date 2026-08-07
    uv run python -m scripts.dry_run_past_session --date 2026-08-07 --kind close --user <uuid>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text

from worker.assemble import assemble
from worker.compute import compute_and_store
from worker.constants import DEV_USER_ID
from worker.db import get_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="session date to assemble, YYYY-MM-DD")
    parser.add_argument("--kind", default="close", choices=("open", "close"))
    parser.add_argument("--user", default=DEV_USER_ID, help="user id (defaults to the dev user)")
    parser.add_argument(
        "--backdate-to",
        default=None,
        help="opened_on to use for every lot, YYYY-MM-DD (default: 30 days before --date)",
    )
    args = parser.parse_args()

    session_date = date.fromisoformat(args.date)
    backdate_to = (
        date.fromisoformat(args.backdate_to)
        if args.backdate_to
        else session_date.replace(day=1)
    )

    conn = get_engine().connect()
    trans = conn.begin()
    try:
        lots = conn.execute(
            text("SELECT id, opened_on FROM lots WHERE user_id = :u"), {"u": args.user}
        ).mappings().all()
        if not lots:
            raise SystemExit(f"No lots found for user {args.user}.")

        conn.execute(
            text("UPDATE lots SET opened_on = :d WHERE user_id = :u"),
            {"d": backdate_to, "u": args.user},
        )
        print(
            f"(txn-local) backdated {len(lots)} lot(s) to {backdate_to} "
            f"for session {session_date}"
        )

        result = compute_and_store(conn, args.user, session_date)
        if not result.positions:
            raise SystemExit(f"No open positions for {session_date}. Try an earlier --backdate-to.")

        closes: dict[str, Decimal] = {
            p.symbol: Decimal(
                str(
                    conn.execute(
                        text("SELECT c FROM bars_daily WHERE symbol = :s AND session_date = :d"),
                        {"s": p.symbol, "d": session_date},
                    ).scalar_one()
                )
            )
            for p in result.positions
        }

        obj = assemble(
            result,
            closes,
            user_id=args.user,
            session_date=session_date,
            kind=args.kind,
            generated_at=datetime.now(UTC),
        )
        print(json.dumps(obj.model_dump(mode="json"), indent=2))
    finally:
        trans.rollback()
        conn.close()
        print("\nrolled back — no lots or briefs rows were changed.")


if __name__ == "__main__":
    main()
