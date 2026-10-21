"""options: contracts, snapshots, the strip, and index_daily (M21)

`option_contracts` is the identity of a listed contract (OCC symbol, expiry,
side, strike). `option_snapshot` is one row per contract per session, holding
only the contracts the strip and IV30 selected (about thirty per name), not the
whole chain; the full chain lives in `raw_payloads` inside the D38 band.
`option_strip` is the derived tenor-by-strike-rule grid the brief reads, and
`index_daily` holds index closes, which have no adjusted series and no volume.

Like the other market-data tables these carry no `user_id`: they are shared and
keyed by symbol, so invariant 4 does not apply. RLS is enabled with a public
read policy, mirroring `bars_daily_read` in 0003.

Revision ID: 0020_options
Revises: 0019_bars_adjusted
Create Date: 2026-10-20

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_options"
down_revision: str | None = "0019_bars_adjusted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE option_contracts (
            contract        text PRIMARY KEY,
            symbol          text NOT NULL,
            expiration_date date NOT NULL,
            put_or_call     text NOT NULL CHECK (put_or_call IN ('C', 'P')),
            strike          numeric NOT NULL,
            first_seen      date NOT NULL
        );
        CREATE INDEX option_contracts_symbol_expiry_idx
            ON option_contracts (symbol, expiration_date);

        CREATE TABLE option_snapshot (
            contract        text NOT NULL REFERENCES option_contracts (contract),
            session_date    date NOT NULL,
            bid             numeric NOT NULL,
            bid_size        integer,
            ask             numeric NOT NULL,
            ask_size        integer,
            last            numeric,
            iv              numeric,
            delta           numeric,
            gamma           numeric,
            theta           numeric,
            vega            numeric,
            rho             numeric,
            open_interest   integer NOT NULL,
            volume          integer NOT NULL,
            prev_day_close  numeric,
            captured_at     timestamptz NOT NULL,
            PRIMARY KEY (contract, session_date)
        );

        CREATE TABLE option_strip (
            symbol           text NOT NULL,
            session_date     date NOT NULL,
            tenor            smallint NOT NULL,
            strike_rule      text NOT NULL CHECK (strike_rule IN ('itm10', 'atm', 'otm10')),
            contract         text,
            dte              smallint,
            moneyness        numeric,
            mid              numeric,
            premium_pct      numeric,
            time_value_pct   numeric,
            iv               numeric,
            delta            numeric,
            open_interest    integer,
            volume           integer,
            chg_contract_pct numeric,
            chg_tenor_pct    numeric,
            iv_chg           numeric,
            tenor_collapsed  boolean NOT NULL DEFAULT false,
            target_miss      boolean NOT NULL DEFAULT false,
            PRIMARY KEY (symbol, session_date, tenor, strike_rule)
        );

        CREATE TABLE index_daily (
            symbol       text NOT NULL,
            session_date date NOT NULL,
            o            numeric,
            h            numeric,
            l            numeric,
            c            numeric NOT NULL,
            PRIMARY KEY (symbol, session_date)
        );
    """)
    op.execute("""
        ALTER TABLE option_contracts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE option_snapshot  ENABLE ROW LEVEL SECURITY;
        ALTER TABLE option_strip     ENABLE ROW LEVEL SECURITY;
        ALTER TABLE index_daily      ENABLE ROW LEVEL SECURITY;

        CREATE POLICY option_contracts_read ON option_contracts FOR SELECT USING (true);
        CREATE POLICY option_snapshot_read  ON option_snapshot  FOR SELECT USING (true);
        CREATE POLICY option_strip_read     ON option_strip     FOR SELECT USING (true);
        CREATE POLICY index_daily_read      ON index_daily      FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS index_daily;
        DROP TABLE IF EXISTS option_strip;
        DROP TABLE IF EXISTS option_snapshot;
        DROP TABLE IF EXISTS option_contracts;
    """)
