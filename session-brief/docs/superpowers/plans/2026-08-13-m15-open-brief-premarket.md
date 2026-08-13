# M15 — Open brief: overnight tape + pre-market names — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the open brief's §2 (overnight tape) and §3 (your names, pre-market) from synthetically-seeded quote data behind the `MarketDataProvider` seam, add the §5 pre-market column and §1 gap salience, and emit a horizon-0 morning claim that the same session's close brief resolves.

**Architecture:** A new `quotes` table (shared, no `user_id`) holds one pre-open capture per symbol per session — held names in `extended_last`/`extended_v`, macro tape symbols in `last`/`prev_close`. One ingest function (`worker/premarket.py::ingest_premarket`) writes it, fed by either `SyntheticPremarketProvider` or a live `FdnProvider`; both implement the same four protocol methods, which is what makes the swap a constructor change. `assemble_open.py` grows two section builders over pure inputs; `claims.py` grows a horizon-0 path. The close assembler, the P&L compute path, and the close renderer are untouched.

**Tech Stack:** Python 3.12 (`uv`, SQLAlchemy Core, Pydantic, pytest), Alembic, Postgres 16, Next.js 15 / React Email, JSON Schema + `pnpm contracts:gen`.

---

## Feasibility: yes, now — off M14, not off main

**M15 does not depend on M12 or M13.** Its data path (`quotes`, pre-market, futures) is disjoint from attribution: no shared table, no shared module, no shared read. The only M13 touchpoint the spec names — upgrading §1 salience from "largest pre-market gap" to "largest overnight `|resid_z|`" — is explicitly out of scope for M15 ("cross-references M13 but doesn't depend on it").

**M15 does depend on M14**, which is complete on `worktree-m14-open-brief-skeleton` / `origin/feat/m14-open-brief-skeleton` (195 tests pass) but **not merged to `main`**. Branch M15 off M14's tip, not off `main`.

Four coordination costs, all real but none blocking:

1. **`schema_version` numbering.** M13 and M15 both bump. M14 took 3, so this plan takes **4**. If M13 merges first, M15 becomes 5 — a one-line constant change plus regenerated fixtures. Keep the version in `SCHEMA_VERSION` (already centralized in `assemble.py`) and never inline it in assembly code.
2. **Alembic head collision — already present between M12 and M14.** Both branches add a `0010_*` revision with `down_revision = "0009_attribution"`: M12's `0010_attribution_econometrics`, M14's `0010_open_events`. Merging both yields two heads. That is M14's merge problem, but M15 inherits it: this plan's `0011_quotes` chains off `0010_open_events`, and whoever merges M12 must relink one chain (or add a merge revision). Do not "fix" it inside M15.
3. **`providers/base.py` and `providers/fdn.py` conflict textually with M12**, which adds methods to both files. Trivial to resolve; append rather than reorder.
4. **M10's box stays unticked** (the five-session live soak). Irrelevant to M15 — the scheduler code it needs is in place.

## Spec corrections (verified against the tree)

The design spec (`docs/superpowers/specs/2026-08-13-m15-open-brief-premarket-design.md`) makes four claims that do not hold. This plan supersedes them; fold these into the spec in Task 11.

1. **"Both feeds land in the existing shared `quotes` table … No new tables; no Alembic migration for the feeds." — false.** `quotes` has never existed. `0003_market_data` created only `raw_payloads` and `bars_daily`; `docs/03-data-model.md` sketches `quotes` but nothing implements it, and no code references it. **M15 needs a migration.** (Same shape of error M14 found with `events`.)
2. **"the existing unique key" for claim idempotency, at horizon 0 — insufficient.** The contract pins `horizon_sessions` to `"minimum": 1`, the `claim.type` enum has no `premarket_gap`, `claims.resolve_due_claims` reads only `session_date < :session_date` (excludes same-day), and `_resolve_session` offsets by `horizon - 1` (i.e. `-1` at horizon 0). Horizon 0 is a real engine change, not a free ride on the seam. The `UNIQUE (user_id, symbol, claim_type, session_date)` key *is* enough for idempotency once the type is new.
3. **"§3: names moving >1% pre-market **or carrying news**" — half unbuildable.** There is no `news_items` table and no news feed (docs/02: FinancialData.net `get_latest_news`, Premium, unwired). Build the threshold predicate to take a `has_news: bool`, wire nothing to it, and document the gap — the D18 precedent, where `short_interest` has a live threshold and no feed.
4. **"§2/§3 section IDs already exist in the enum (M14 note)" — true, and that's all that exists.** The `row` def has no §2 or §3 fields; §5's `premarket` field does exist (null since M14). The bump is for §2's `level`/`overnight_pct`/`overnight_abs`, §3's `pre_pct`/`gap_cents`/`premarket_vol_mult`, and the two `claim` changes.

## Global Constraints

- **Money is integer cents at rest and `Decimal` in Python. Never float on the money path.** `gap_cents` is an integer; prices are `Decimal`; ratios (`pre_pct`, `premarket_vol_mult`, `overnight_pct`) cross into the object as floats, which is the existing rule for display ratios (`assemble_open._float`).
- **The LLM never produces a number** (invariant 2). Every narration string containing a digit is dropped at the parser.
- **Market-data tables carry no `user_id`** (D21/D18 precedent): `quotes` is keyed by symbol and shared.
- **Python owns the schema.** Alembic only; no DDL from the web app.
- **Trading days come from `exchange_calendars`** (invariant 7); never hardcode a session.
- **`generated_at` is injected, never `datetime.now()`** inside assembly (D15/D21).
- **`schema_version` = 4** for this milestone, read from `worker.assemble.SCHEMA_VERSION`. Old renderers stay; `close_brief_v2.json` and a new frozen `open_brief_v3.json` must keep validating.
- **`pnpm contracts:gen` must be clean** at the end of every task that touches `packages/contracts/brief-object.schema.json`.
- **The close pipeline is untouched.** `assemble.py`, `compute.py`, `tape.py`, `close-brief.tsx` get no behavioural change. The one exception is `claims.py`, shared by both — every change there must leave the horizon-1 close loop byte-identical, proven by the existing `test_claims.py` / `test_claims_db.py` staying green unmodified.
- **The §3 volume multiple is pre-market-specific**, computed from prior sessions' `extended_v`, never from `bars_daily.v` or the 30-day RVOL (D3, docs/05: "pre-market volume is too thin for RVOL to mean anything").
- **Both the seed and the live path go through one ingest function.** A section must never learn which provider filled `quotes`.
- Run everything from `session-brief/`: `uv run pytest`, `uv run alembic upgrade head`, `pnpm contracts:gen`, `pnpm --filter web typecheck`.

## File structure

**Create**
- `apps/worker/alembic/versions/0011_quotes.py` — the `quotes` table (shared, RLS-read, PK `(symbol, session_date)`).
- `apps/worker/worker/premarket.py` — pure pre-market/tape math (`pre_pct`, `gap_cents`, `premarket_vol_mult`, threshold predicate) **and** the DB layer that ingests provider output into `quotes` and reads it back. One responsibility: pre-market data, from vendor shape to assembler input.
- `apps/worker/worker/providers/synthetic.py` — `SyntheticPremarketProvider`, deterministic, no network, no DB.
- `apps/worker/tests/test_premarket.py` — the pure math and threshold.
- `apps/worker/tests/test_premarket_db.py` — ingest → `quotes` → read-back, and provider parity (DoD 5).
- `apps/worker/tests/test_claims_horizon0.py` — the morning claim, pure emission + same-day resolution.
- `apps/worker/tests/fixtures/open_brief_v3.json` — the M14-era open body, frozen for back-compat.

**Modify**
- `packages/contracts/brief-object.schema.json` — §2/§3 row fields; `claim.horizon_sessions` minimum 0; `claim.type` gains `premarket_gap`. Regenerates `apps/worker/contracts/brief.py` + `apps/web/lib/contracts/brief.ts`.
- `apps/worker/worker/constants.py` — tape symbols, foreign proxies, thresholds.
- `apps/worker/worker/config.py` — pre-market capture time.
- `apps/worker/worker/providers/base.py`, `providers/fdn.py` — the four premium methods.
- `apps/worker/worker/assemble_open.py` — §2, §3, §5's pre-market column, §1 ordering, claim emission, the new reads.
- `apps/worker/worker/claims.py` — `emit_premarket_gap` + the horizon-0 resolution path.
- `apps/worker/worker/narrate.py` — `tape_read`, `why` over premarket rows.
- `apps/worker/worker/scheduler.py` — the 08:00 pre-open ingest stage.
- `apps/worker/worker/cli.py` — `seed-premarket` command.
- `apps/web/emails/open-brief.tsx`, `apps/web/emails/open-brief-preview.tsx`, `apps/web/app/briefs/[slug]/page.tsx` — render §2/§3.
- `apps/worker/tests/test_assemble_open.py`, `test_assemble_open_db.py`, `test_contract_open.py`, `test_narrate.py`, `test_scheduler_open.py`, `tests/fixtures/open_brief.json`.
- `docs/03-data-model.md`, `docs/04-brief-object.md`, `docs/07-decisions.md`, `docs/08-milestones.md`, the M15 spec.

---

### Task 1: The `quotes` table

**Files:**
- Create: `apps/worker/alembic/versions/0011_quotes.py`
- Create: `apps/worker/tests/test_premarket_db.py`
- Modify: `docs/03-data-model.md:19-34`

**Interfaces:**
- Consumes: nothing.
- Produces: table `quotes (symbol text, session_date date, captured_at timestamptz, last numeric, prev_close numeric, extended_last numeric, extended_v bigint)`, PK `(symbol, session_date)`.

The docs/03 sketch keys `quotes` by `(symbol, captured_at)`. That is wrong for this use: every read is "the pre-open capture for session D", and a timestamp key makes both the read and the upsert awkward. Key it `(symbol, session_date)` — one capture per symbol per session, idempotent re-seeding — and keep `captured_at` as an attribute, because §3's header renders it ("Your names, pre-market · 08:12 ET").

- [ ] **Step 1: Write the failing test**

```python
# apps/worker/tests/test_premarket_db.py
"""The `quotes` table and the pre-market ingest path (M15), against a real
database (skipped without DATABASE_URL). Rolled back per test."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

_SESSION = date(2098, 4, 7)
_CAPTURED = datetime(2098, 4, 7, 12, 12, tzinfo=UTC)


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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_premarket_db.py -v` from `apps/worker`
Expected: FAIL — `relation "quotes" does not exist` (or SKIP if `DATABASE_URL` is unset; set it before this task, the whole task is a DB change).

- [ ] **Step 3: Write the migration**

```python
# apps/worker/alembic/versions/0011_quotes.py
"""quotes: the pre-open capture the open brief's §2/§3 read (M15)

`docs/03-data-model.md` has sketched this table since day one, but nothing ever
created it — `0003_market_data` built only raw_payloads and bars_daily, because
everything through M14 runs on daily bars. §2 (overnight macro tape) and §3
(your names, pre-market) are the first readers.

Two deviations from the docs/03 sketch, both deliberate:

- **Keyed `(symbol, session_date)`, not `(symbol, captured_at)`.** Every read is
  "the pre-open capture for session D". A timestamp key would make that a range
  scan and make re-seeding duplicate rather than replace. `captured_at` stays as
  an attribute because §3's header renders it.
- **No `user_id`.** Market data is shared and keyed by symbol (D18/D21), which is
  what keeps ingest cost per-symbol rather than per-user.

Held names land in `extended_last`/`extended_v` (the pre-market print and the
summed pre-market volume); the macro tape symbols land in `last`/`prev_close`.
One table, because both are "a quote for a symbol, captured before the open".

Revision ID: 0011_quotes
Revises: 0010_open_events
Create Date: 2026-08-13

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_quotes"
down_revision: str | None = "0010_open_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE quotes (
            symbol        text NOT NULL,
            session_date  date NOT NULL,
            captured_at   timestamptz NOT NULL,
            last          numeric,
            prev_close    numeric,
            extended_last numeric,
            extended_v    bigint,
            PRIMARY KEY (symbol, session_date)
        );
        CREATE INDEX quotes_session_idx ON quotes (session_date);
    """)

    # Shared reference data: RLS on with a read-only policy, exactly as
    # 0003_market_data does for bars_daily. The worker writes as table owner.
    op.execute("""
        ALTER TABLE quotes ENABLE ROW LEVEL SECURITY;
        CREATE POLICY quotes_read ON quotes FOR SELECT USING (true);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quotes;")
```

- [ ] **Step 4: Apply and run the tests**

Run: `uv run alembic upgrade head && uv run pytest tests/test_premarket_db.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Update the data-model doc**

In `docs/03-data-model.md`, replace the `quotes` line in the Market data block:

```sql
quotes       (symbol, session_date, captured_at, last, prev_close, extended_last, extended_v)
             -- PK (symbol, session_date); one pre-open capture per symbol per session (M15)
```

and add below the block:

> `quotes` holds the pre-open capture the open brief's §2/§3 read: held names in
> `extended_last`/`extended_v` (pre-market print, summed pre-market volume), macro
> tape symbols in `last`/`prev_close`. It is keyed by session rather than by
> capture timestamp — every read is "the capture for session D", and the session
> key is what makes re-seeding idempotent.

- [ ] **Step 6: Commit**

```bash
git add apps/worker/alembic/versions/0011_quotes.py apps/worker/tests/test_premarket_db.py docs/03-data-model.md
git commit -m "feat(m15): migration 0011 — the quotes table docs/03 has always sketched"
```

---

### Task 2: Contract v4 — §2/§3 row fields and the horizon-0 claim

**Files:**
- Modify: `packages/contracts/brief-object.schema.json:69-166`
- Modify: `apps/worker/worker/assemble.py` (the `SCHEMA_VERSION` constant)
- Modify: `apps/worker/tests/test_contract_open.py`
- Create: `apps/worker/tests/fixtures/open_brief_v3.json`
- Generated (do not hand-edit): `apps/worker/contracts/brief.py`, `apps/web/lib/contracts/brief.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `row.level`, `row.overnight_pct`, `row.overnight_abs`, `row.pre_pct`, `row.gap_cents`, `row.premarket_vol_mult` (all nullable); `claim.horizon_sessions` minimum 0; `claim.type` enum member `premarket_gap`; `SCHEMA_VERSION == 4`.

- [ ] **Step 1: Freeze the M14-era open body**

```bash
cp apps/worker/tests/fixtures/open_brief.json apps/worker/tests/fixtures/open_brief_v3.json
```

This is the back-compat witness (DoD 6), the `close_brief_v2.json` precedent: a real v3 body, frozen before the bump and never regenerated.

- [ ] **Step 2: Write the failing tests**

Append to `apps/worker/tests/test_contract_open.py`:

```python
_V3_OPEN_FIXTURE = Path(__file__).parent / "fixtures" / "open_brief_v3.json"


def test_v3_open_body_still_validates() -> None:
    """A stored M14-era open brief must keep loading after the v4 bump
    (docs/04: keep old renderers, never break stored bodies)."""
    body = json.loads(_V3_OPEN_FIXTURE.read_text())
    assert body["schema_version"] == 3
    assert BriefObject.model_validate(body).kind.value == "open"


def test_overnight_tape_row_fields() -> None:
    """§2 carries a level and an overnight change; the change is a percent for
    price-quoted symbols and an absolute for level-quoted ones (10Y, VIX)."""
    payload = _minimal(
        sections=[{
            "id": "overnight_tape",
            "tier": "full",
            "rows": [
                {"symbol": "ES=F", "label": "ES futures", "level": 5612.25,
                 "overnight_pct": -0.0041, "overnight_abs": -23.0},
                {"symbol": "^TNX", "label": "10Y", "level": 4.28,
                 "overnight_pct": None, "overnight_abs": 0.03},
            ],
        }]
    )
    obj = BriefObject.model_validate(payload)
    assert obj.sections[0].rows[1].overnight_pct is None


def test_premarket_row_fields() -> None:
    """§3 carries pre %, the gap in integer cents, and a pre-market-specific
    volume multiple — never the 30-day RVOL."""
    payload = _minimal(
        sections=[{
            "id": "premarket",
            "tier": "full",
            "rows": [{"symbol": "SNDK", "pre_pct": 0.041, "gap_cents": 194,
                      "premarket_vol_mult": 3.1, "why": None}],
        }]
    )
    row = BriefObject.model_validate(payload).sections[0].rows[0]
    assert row.gap_cents == 194
    assert row.rvol is None


def test_horizon_zero_premarket_gap_claim() -> None:
    """The morning claim: a new type, resolved the same session (horizon 0)."""
    payload = _minimal(
        claims=[{"id": "c1", "symbol": "SNDK", "type": "premarket_gap",
                 "direction": "up", "horizon_sessions": 0, "outcome": None}]
    )
    claim = BriefObject.model_validate(payload).claims[0]
    assert claim.horizon_sessions == 0


def test_schema_version_is_four() -> None:
    from worker.assemble import SCHEMA_VERSION

    assert SCHEMA_VERSION == 4
```

Also extend `apps/worker/tests/test_contract_schema.py`'s fixture list so the new `open_brief_v3.json` is validated against `brief-object.schema.json` directly (follow the pattern already in that file — it iterates the fixtures directory or a literal list; add the file to whichever it uses).

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_contract_open.py -v`
Expected: FAIL — `ValidationError: Extra inputs are not permitted` on `level`/`pre_pct`/…, and `horizon_sessions … greater than or equal to 1`.

- [ ] **Step 4: Edit the JSON Schema**

In `packages/contracts/brief-object.schema.json`, inside `$defs.row.properties`, after the `premarket` property:

```json
        "level": {
          "type": ["number", "null"],
          "description": "Open brief §2 (overnight tape): the symbol's overnight level — a futures price, a yield, an index level (M15)."
        },
        "overnight_pct": {
          "type": ["number", "null"],
          "description": "Open brief §2: overnight change as a fraction, last vs prior close. Null for level-quoted symbols (10Y, VIX), where a percent change reads as noise and the absolute move is the figure (M15)."
        },
        "overnight_abs": {
          "type": ["number", "null"],
          "description": "Open brief §2: overnight change in the symbol's own units — index points, yield points, volatility points (M15)."
        },
        "pre_pct": {
          "type": ["number", "null"],
          "description": "Open brief §3 (pre-market): extended_last vs prior close, as a fraction (M15)."
        },
        "gap_cents": {
          "type": ["integer", "null"],
          "description": "Open brief §3: the pre-market gap in integer cents. Dollars, not percent — a percent tells you nothing about what the position is worth (docs/01, M15)."
        },
        "premarket_vol_mult": {
          "type": ["number", "null"],
          "description": "Open brief §3: pre-market volume against the typical pre-market volume at the same point in the morning. Deliberately NOT `rvol` — pre-market volume is too thin for a 30-day daily-volume ratio to mean anything (D3, docs/05, M15)."
        }
```

In `$defs.claim.properties`:

```json
        "type": { "enum": ["catalyst_pending", "relative_strength",
                           "supply_overhang", "breadth", "premarket_gap"] },
        "direction": { "enum": ["up", "down", "neutral"] },
        "horizon_sessions": {
          "type": "integer",
          "minimum": 0,
          "description": "Sessions until the claim is due. 0 is the open brief's morning claim, resolved at the same session's close — the same-day loop D16b built the engine for (M15)."
        },
```

- [ ] **Step 5: Bump the version constant**

In `apps/worker/worker/assemble.py`, change `SCHEMA_VERSION = 3` to:

```python
# v4 (M15): §2/§3 row fields and the horizon-0 morning claim. Coordinate with
# M13, which also bumps — landing order assigns the number (D22).
SCHEMA_VERSION = 4
```

- [ ] **Step 6: Regenerate and run**

Run: `pnpm contracts:gen && cd apps/worker && uv run pytest -q && cd ../.. && pnpm --filter web typecheck`
Expected: contracts regenerate, full suite passes except the frozen `open_brief.json` snapshot (which now reports version 3 vs 4) — regenerate that fixture from the snapshot test's own instructions (`test_assemble_open.py` writes it when absent; delete and re-run, then diff to confirm only `schema_version` changed).

- [ ] **Step 7: Commit**

```bash
git add packages/contracts apps/worker/contracts apps/web/lib/contracts apps/worker/worker/assemble.py apps/worker/tests
git commit -m "feat(m15): schema_version 4 — §2/§3 row fields + the horizon-0 claim"
```

---

### Task 3: The provider seam and the synthetic feed

**Files:**
- Modify: `apps/worker/worker/providers/base.py`
- Modify: `apps/worker/worker/providers/fdn.py`
- Create: `apps/worker/worker/providers/synthetic.py`
- Modify: `apps/worker/worker/constants.py`
- Create: `apps/worker/tests/test_premarket.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MarketDataProvider.get_latest_prices(symbols: list[str]) -> list[dict[str, Any]]` → `{"symbol": str, "extended_last": Decimal, "extended_v": int, "prev_close": Decimal}`
  - `MarketDataProvider.get_futures_prices(symbols) / get_index_quotes(symbols) / get_forex_quotes(symbols) -> list[dict[str, Any]]` → `{"symbol": str, "last": Decimal, "prev_close": Decimal}`
  - `SyntheticPremarketProvider(prior_closes: dict[str, Decimal], session_date: date)` implementing all four.
  - `constants.TAPE_SYMBOLS: tuple[tuple[str, str, str], ...]`, `constants.LEVEL_QUOTED: frozenset[str]`, `constants.FOREIGN_PROXIES: dict[str, tuple[str, str]]`, `constants.PREMARKET_THRESHOLD: Decimal`.

- [ ] **Step 1: Write the failing test**

```python
# apps/worker/tests/test_premarket.py
"""The pre-market feed seam and its pure math (M15).

Everything here runs without a network or a database: the synthetic provider is
deterministic by construction, which is what lets a seeded pre-market session be
snapshot-tested (the M7 fundamentals / M14 events precedent).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from worker.providers.synthetic import SyntheticPremarketProvider

_SESSION = date(2026, 8, 13)
_CLOSES = {"SNDK": Decimal("47.32"), "SYM": Decimal("36.10"), "ES=F": Decimal("5635.25")}


def test_synthetic_latest_prices_are_deterministic() -> None:
    """Same symbols, same session → byte-identical output. A seeded feed that
    drifted between runs would make every snapshot test flaky."""
    a = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK", "SYM"])
    b = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK", "SYM"])
    assert a == b


def test_synthetic_latest_prices_shape() -> None:
    (row,) = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK"])
    assert set(row) == {"symbol", "extended_last", "extended_v", "prev_close"}
    assert isinstance(row["extended_last"], Decimal)  # never float on the price path
    assert isinstance(row["extended_v"], int)
    assert row["prev_close"] == _CLOSES["SNDK"]


def test_synthetic_moves_with_the_session() -> None:
    """A different morning gaps differently — otherwise every seeded session
    tells the same story and the threshold is never exercised."""
    today = SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["SNDK"])
    other = SyntheticPremarketProvider(_CLOSES, date(2026, 8, 14)).get_latest_prices(["SNDK"])
    assert today[0]["extended_last"] != other[0]["extended_last"]


def test_synthetic_tape_shape() -> None:
    (row,) = SyntheticPremarketProvider(_CLOSES, _SESSION).get_futures_prices(["ES=F"])
    assert set(row) == {"symbol", "last", "prev_close"}
    assert isinstance(row["last"], Decimal)


def test_symbol_without_a_prior_close_is_skipped() -> None:
    """No prior close, no gap — a name the book just added has nothing to
    measure against, and inventing a base would invent a move."""
    assert SyntheticPremarketProvider(_CLOSES, _SESSION).get_latest_prices(["NEW"]) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_premarket.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.providers.synthetic'`.

- [ ] **Step 3: Add the constants**

Append to `apps/worker/worker/constants.py`:

```python
from decimal import Decimal

# --- Open brief §2/§3 (M15) -------------------------------------------------

# The overnight macro tape (docs/05 §2), as (symbol, label, feed). `feed` picks
# the provider method, which is what keeps the licensing story honest: futures,
# index and forex are three different premium endpoints.
TAPE_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("ES=F", "ES futures", "futures"),
    ("NQ=F", "NQ futures", "futures"),
    ("^TNX", "10Y", "index"),
    ("DXY", "DXY", "forex"),
    ("^VIX", "VIX", "index"),
    ("CL=F", "WTI", "futures"),
)

# Symbols quoted as a level, not a price: a percent change on a yield or on the
# VIX reads as noise ("the 10Y rose 0.7%"), while the absolute move is the
# figure a reader acts on ("+3bp"). §2 emits `overnight_pct = None` for these.
LEVEL_QUOTED: frozenset[str] = frozenset({"^TNX", "^VIX"})

# Foreign proxies, keyed by the *sector benchmark* the book already stores. This
# is what makes §2 relevant rather than generic (the spec: "chosen from the
# book's sectors") — a book with no semis sleeve gets no Taiwan line.
FOREIGN_PROXIES: dict[str, tuple[str, str]] = {
    "SMH": ("EWT", "Taiwan (semis)"),
    "XLK": ("EWJ", "Japan (tech)"),
    "XLE": ("EWC", "Canada (energy)"),
    "XLF": ("EUFN", "Europe (financials)"),
    "XLI": ("EWG", "Germany (industrials)"),
}

# §3's line: only names moving more than this pre-market get a row (docs/05).
PREMARKET_THRESHOLD: Decimal = Decimal("0.01")
```

- [ ] **Step 4: Extend the protocol**

Append to `apps/worker/worker/providers/base.py`, inside `MarketDataProvider`:

```python
    # --- Pre-market and the overnight tape (open brief §2/§3, M15) ---------
    #
    # Premium-tier, licensing-sensitive data (D8): delayed pre-market quotes and
    # overnight futures/macro. Declared here so the sections are written against
    # the seam; `SyntheticPremarketProvider` satisfies it today and a licensed
    # `FdnProvider` satisfies it later, with no change above this line.

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Pre-market print and summed pre-market volume per symbol. Returns
        ``{"symbol", "extended_last": Decimal, "extended_v": int,
        "prev_close": Decimal}``; symbols with no prior close are omitted."""
        ...

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        """``{"symbol", "last": Decimal, "prev_close": Decimal}`` for futures."""
        ...

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Same shape, for index and yield series (VIX, 10Y)."""
        ...

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Same shape, for currency series (DXY)."""
        ...
```

Append to `apps/worker/worker/providers/fdn.py`, inside `FdnProvider`:

```python
    # The M15 pre-market seam. fdnpy exposes all four (get_latest_prices,
    # get_futures_prices, get_index_quotes, get_forex_quotes) at Premium tier,
    # personal-use-only, with redistribution behind Enterprise (docs/02, D8).
    # Until that is licensed the open brief runs on SyntheticPremarketProvider —
    # a business decision, not a code blocker.

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy latest prices are Premium; M15 seeds synthetically")

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy futures are Premium; M15 seeds synthetically")

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy index quotes are Premium; M15 seeds synthetically")

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError("fdnpy forex is Premium; M15 seeds synthetically")
```

- [ ] **Step 5: Write the synthetic provider**

```python
# apps/worker/worker/providers/synthetic.py
"""A deterministic pre-market feed, standing in for the licensed one (M15).

Pre-market quotes and the overnight macro tape are Premium-tier, redistribution-
gated data (docs/02, D8). Rather than block the sections on procurement, M15
builds them against this provider — the M7 `fundamentals` / M14 `events`
pattern — and swaps to `FdnProvider` once licensed. Both satisfy the same four
`MarketDataProvider` methods, so nothing above the seam changes.

Determinism is the whole design constraint: the gap for a symbol is a pure
function of `(symbol, session_date)` via a hash, never `random`. That is what
makes a seeded morning snapshot-testable, and what makes "re-run the seed" a
no-op rather than a new story.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

# Gaps land in ±3.5%, wide enough that some names clear §3's 1% line and some
# don't — a seed where everything moves would never exercise the suppression.
_GAP_SPAN = Decimal("0.07")
_GAP_CENTER = Decimal("0.035")
# Pre-market volume, as a share of a nominal typical morning. The spread is what
# gives the volume multiple something to say.
_VOL_BASE = 40_000


class SyntheticPremarketProvider:
    """Deterministic pre-market and tape quotes derived from prior closes.

    ``prior_closes`` is read from ``bars_daily`` by the caller — a provider does
    not touch the database. A symbol with no prior close is omitted rather than
    invented: no base, no gap.
    """

    def __init__(self, prior_closes: dict[str, Decimal], session_date: date) -> None:
        self._closes = prior_closes
        self._session = session_date

    # --- the four seam methods --------------------------------------------

    def get_latest_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            prev = self._closes.get(symbol)
            if prev is None:
                continue
            gap = self._unit(symbol, "gap") * _GAP_SPAN - _GAP_CENTER
            out.append({
                "symbol": symbol,
                "extended_last": (prev * (1 + gap)).quantize(Decimal("0.01")),
                "extended_v": int(_VOL_BASE * (Decimal("0.2") + self._unit(symbol, "vol") * 4)),
                "prev_close": prev,
            })
        return out

    def get_futures_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_index_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    def get_forex_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        return self._tape(symbols)

    # --- internals ---------------------------------------------------------

    def _tape(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            prev = self._closes.get(symbol)
            if prev is None:
                continue
            move = self._unit(symbol, "tape") * Decimal("0.02") - Decimal("0.01")
            out.append({
                "symbol": symbol,
                "last": (prev * (1 + move)).quantize(Decimal("0.0001")),
                "prev_close": prev,
            })
        return out

    def _unit(self, symbol: str, salt: str) -> Decimal:
        """A stable value in [0, 1) for this symbol, this session, this axis."""
        digest = hashlib.sha256(
            f"{symbol}|{self._session.isoformat()}|{salt}".encode()
        ).digest()
        return Decimal(int.from_bytes(digest[:4], "big")) / Decimal(1 << 32)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_premarket.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Add a protocol-conformance assertion**

`tests/test_fdn.py` already asserts `FdnProvider` satisfies `MarketDataProvider`. Add the same for the synthetic one, in `tests/test_premarket.py`:

```python
def test_synthetic_provider_satisfies_the_protocol() -> None:
    """The point of the seam: the seed and the licensed feed are interchangeable."""
    from worker.providers.base import MarketDataProvider

    provider: MarketDataProvider = SyntheticPremarketProvider({}, _SESSION)  # type: ignore[assignment]
    assert callable(provider.get_latest_prices)
```

Run: `uv run pytest tests/test_premarket.py tests/test_fdn.py -v` → PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/worker/worker/providers apps/worker/worker/constants.py apps/worker/tests/test_premarket.py
git commit -m "feat(m15): the pre-market provider seam + a deterministic synthetic feed"
```

---

### Task 4: `worker/premarket.py` — ingest, read-back, and the pure math

**Files:**
- Create: `apps/worker/worker/premarket.py`
- Modify: `apps/worker/tests/test_premarket.py`
- Modify: `apps/worker/tests/test_premarket_db.py`
- Modify: `apps/worker/worker/config.py`

**Interfaces:**
- Consumes: `SyntheticPremarketProvider` / `MarketDataProvider` (Task 3), `quotes` (Task 1).
- Produces:
  - `@dataclass(frozen=True) PremarketQuote(symbol: str, extended_last: Decimal, extended_v: int, prev_close: Decimal, typical_v: Decimal | None)`
  - `@dataclass(frozen=True) TapeQuote(symbol: str, label: str, last: Decimal, prev_close: Decimal)`
  - `pre_pct(q: PremarketQuote) -> Decimal | None`
  - `gap_cents(q: PremarketQuote) -> int`
  - `premarket_vol_mult(q: PremarketQuote) -> Decimal | None`
  - `clears_threshold(q: PremarketQuote, *, has_news: bool = False) -> bool`
  - `ingest_premarket(conn, provider, *, held, tape, session_date, captured_at) -> int`
  - `read_premarket(conn, symbols, session_date) -> list[PremarketQuote]`
  - `read_tape(conn, session_date, sector_benchmarks) -> list[TapeQuote]`
  - `prior_closes(conn, symbols, prior_session) -> dict[str, Decimal]`

- [ ] **Step 1: Write the failing pure tests**

Append to `apps/worker/tests/test_premarket.py`:

```python
from worker.premarket import (
    PremarketQuote,
    clears_threshold,
    gap_cents,
    pre_pct,
    premarket_vol_mult,
)


def _q(last: str, prev: str, v: int = 100_000, typical: str | None = "50000") -> PremarketQuote:
    return PremarketQuote(
        symbol="SNDK",
        extended_last=Decimal(last),
        extended_v=v,
        prev_close=Decimal(prev),
        typical_v=Decimal(typical) if typical is not None else None,
    )


def test_pre_pct_is_extended_last_against_the_prior_close() -> None:
    assert pre_pct(_q("50.00", "40.00")) == Decimal("0.25")


def test_pre_pct_is_none_without_a_base() -> None:
    assert pre_pct(_q("50.00", "0")) is None


def test_gap_is_dollars_in_integer_cents() -> None:
    """The dollars-not-percent rule (docs/01): +$1.94 on a 47.32 close."""
    assert gap_cents(_q("49.26", "47.32")) == 194
    assert gap_cents(_q("46.00", "47.32")) == -132


def test_volume_multiple_is_against_typical_premarket_volume() -> None:
    """Not the 30-day RVOL: the base is prior sessions' pre-market volume at the
    same point in the morning, because pre-market volume is too thin for a
    daily-volume ratio to mean anything (D3, docs/05)."""
    assert premarket_vol_mult(_q("50.00", "49.00", v=150_000, typical="50000")) == Decimal("3")


def test_volume_multiple_is_none_without_enough_history() -> None:
    assert premarket_vol_mult(_q("50.00", "49.00", typical=None)) is None


def test_threshold_takes_names_over_one_percent() -> None:
    assert clears_threshold(_q("48.00", "47.32")) is True    # +1.4%
    assert clears_threshold(_q("47.50", "47.32")) is False   # +0.4%
    assert clears_threshold(_q("46.60", "47.32")) is True    # -1.5%, direction-blind


def test_news_clears_the_threshold_on_its_own() -> None:
    """docs/05: ">1% pre-market **or** carrying news". No news feed exists
    (docs/02 marks it Premium and unwired), so the predicate takes the flag and
    nothing sets it yet — the D18 short_interest precedent."""
    assert clears_threshold(_q("47.35", "47.32"), has_news=True) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_premarket.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.premarket'`.

- [ ] **Step 3: Add the capture-time config**

Append to `apps/worker/worker/config.py`:

```python
# The pre-market capture (M15). docs/02 stages the morning ingest at 08:00 ET;
# the capture stamp is what §3's header renders ("pre-market · 08:12 ET"), so it
# is configurable rather than derived from whenever the job happened to run.
PREMARKET_CAPTURE_ET_HOUR: int = int(os.environ.get("PREMARKET_CAPTURE_ET_HOUR", "8"))
PREMARKET_CAPTURE_ET_MINUTE: int = int(os.environ.get("PREMARKET_CAPTURE_ET_MINUTE", "12"))
# How many prior sessions of pre-market volume the §3 multiple averages, and the
# minimum it needs before it will report one. Short by design: pre-market volume
# regime-shifts with the news cycle, so a 30-day base would smear it.
PREMARKET_VOL_WINDOW: int = int(os.environ.get("PREMARKET_VOL_WINDOW", "10"))
PREMARKET_VOL_MIN_OBS: int = int(os.environ.get("PREMARKET_VOL_MIN_OBS", "5"))
```

- [ ] **Step 4: Write the module**

```python
# apps/worker/worker/premarket.py
"""The pre-market feed: vendor shape → `quotes` → the open brief's §2/§3 (M15).

Kept out of `assemble_open.py` for the reason `tape.py` is kept out of
`compute.py`: the assembler should read a value, not know how it was measured.

Two rules earn their own module here:

- **The volume multiple is pre-market-specific.** It compares this morning's
  pre-market volume with the same measure on prior mornings, never with the
  30-day daily RVOL. Pre-market volume is a different, far thinner series, and a
  daily-volume ratio over it is a number that looks meaningful and isn't (D3,
  docs/05).
- **The gap is dollars.** A percent tells you how the stock moved; cents tell you
  what the position did, which is the figure you act on (docs/01).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from worker import config
from worker.constants import PREMARKET_THRESHOLD, TAPE_SYMBOLS
from worker.providers.base import MarketDataProvider


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

    ``has_news`` is a live predicate with no feed behind it — company news is
    Premium and unwired (docs/02), the same shape as `short_interest`'s
    threshold-without-a-source in D18. Wire a news source and this clause starts
    working with no change here.
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

_UPSERT = text("""
    INSERT INTO quotes (symbol, session_date, captured_at, last, prev_close,
                        extended_last, extended_v)
    VALUES (:symbol, :session_date, :captured_at, :last, :prev_close,
            :extended_last, :extended_v)
    ON CONFLICT (symbol, session_date) DO UPDATE
        SET captured_at = EXCLUDED.captured_at,
            last = EXCLUDED.last,
            prev_close = EXCLUDED.prev_close,
            extended_last = EXCLUDED.extended_last,
            extended_v = EXCLUDED.extended_v
""")

_READ_PRIOR_CLOSES = text("""
    SELECT symbol, c FROM bars_daily
    WHERE session_date = :prior_session AND symbol = ANY(:symbols)
""")

_READ_PREMARKET = text("""
    SELECT symbol, extended_last, extended_v, prev_close FROM quotes
    WHERE session_date = :session_date AND symbol = ANY(:symbols)
      AND extended_last IS NOT NULL
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
      AND last IS NOT NULL
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
    provider: MarketDataProvider,
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
        conn.execute(_UPSERT, {
            "symbol": record["symbol"],
            "session_date": session_date,
            "captured_at": captured_at,
            "last": None,
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
            conn.execute(_UPSERT, {
                "symbol": record["symbol"],
                "session_date": session_date,
                "captured_at": captured_at,
                "last": record["last"],
                "prev_close": record["prev_close"],
                "extended_last": None,
                "extended_v": None,
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
```

- [ ] **Step 5: Run the pure tests**

Run: `uv run pytest tests/test_premarket.py -v`
Expected: PASS.

- [ ] **Step 6: Write the DB round-trip and provider-parity test**

Append to `apps/worker/tests/test_premarket_db.py`:

```python
def test_ingest_then_read_round_trips(db_conn: Connection) -> None:
    from worker.premarket import (
        capture_stamp, ingest_premarket, prior_closes, read_premarket, read_tape,
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
        db_conn, MockLive(),  # type: ignore[arg-type]
        held=["ZLIVE"], tape=[], session_date=_SESSION,
        captured_at=capture_stamp(_SESSION),
    )
    (quote,) = read_premarket(db_conn, ["ZLIVE"], _SESSION)
    assert quote.extended_last == Decimal("11.00")
    assert quote.extended_v == 999
```

Add the bars helper at the top of the file (the `test_assemble_open_db.py` style):

```python
def _seed_bars(conn: Connection) -> None:
    for symbol, close in (("ZHELD", "10.00"), ("ES=F", "5600.00")):
        conn.execute(
            text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                 "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"),
            {"s": symbol, "d": _PRIOR, "c": Decimal(close)},
        )
```

with `_PRIOR = date(2098, 4, 6)` beside the existing module constants.

- [ ] **Step 7: Run the DB tests**

Run: `uv run pytest tests/test_premarket_db.py -v`
Expected: PASS (4 tests) with `DATABASE_URL` set.

- [ ] **Step 8: Commit**

```bash
git add apps/worker/worker/premarket.py apps/worker/worker/config.py apps/worker/tests/test_premarket.py apps/worker/tests/test_premarket_db.py
git commit -m "feat(m15): pre-market ingest + the pre-market-specific volume multiple"
```

---

### Task 5: §2 — the overnight tape

**Files:**
- Modify: `apps/worker/worker/assemble_open.py`
- Modify: `apps/worker/tests/test_assemble_open.py`

**Interfaces:**
- Consumes: `premarket.TapeQuote`, `premarket.tape_change` (Task 4).
- Produces: `assemble_open(..., tape: list[TapeQuote], ...)` emitting the `overnight_tape` section with rows `{symbol, label, level, overnight_pct, overnight_abs}`; `_OMITTED_TAPE` is used only when `tape` is empty.

- [ ] **Step 1: Write the failing test**

Append to `apps/worker/tests/test_assemble_open.py`:

```python
from worker.premarket import TapeQuote


def _tape() -> list[TapeQuote]:
    return [
        TapeQuote("ES=F", "ES futures", Decimal("5612.25"), Decimal("5635.25")),
        TapeQuote("^TNX", "10Y", Decimal("4.28"), Decimal("4.25")),
    ]


def test_overnight_tape_carries_level_and_change() -> None:
    obj = _assemble(tape=_tape())
    section = _section(obj, "overnight_tape")
    assert section.tier.value == "full"
    es, tnx = section.rows
    assert es.label == "ES futures"
    assert es.level == 5612.25
    assert es.overnight_pct is not None and es.overnight_pct < 0
    assert es.overnight_abs == -23.0


def test_level_quoted_symbols_report_no_percent() -> None:
    """A percent change on a yield reads as noise; the absolute move is the
    figure (+3bp), so §2 leaves overnight_pct null for 10Y and VIX."""
    _es, tnx = _section(_assemble(tape=_tape()), "overnight_tape").rows
    assert tnx.overnight_pct is None
    assert round(tnx.overnight_abs, 4) == 0.03


def test_an_empty_tape_still_says_so() -> None:
    """No feed, no silence: the M5 precedent — a short brief names what it left
    out rather than quietly shrinking."""
    section = _section(_assemble(tape=[]), "overnight_tape")
    assert section.tier.value == "suppressed"
    assert section.note is not None
```

Add the two helpers next to the existing fixtures in that file (the file already builds objects inline; factor the call once):

```python
def _assemble(**overrides: object) -> BriefObject:
    kwargs: dict[str, object] = {
        "events": [], "sectors": [], "flags": [], "holdings": {},
        "tape": [], "premarket": [], "claims": [],
        "user_id": _USER, "session_date": _SESSION, "prior_session": _PRIOR,
        "generated_at": _GENERATED_AT,
    }
    kwargs.update(overrides)
    return assemble_open(**kwargs)  # type: ignore[arg-type]


def _section(obj: BriefObject, section_id: str):
    return next(s for s in obj.sections if s.id.value == section_id)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_assemble_open.py -k tape -v`
Expected: FAIL — `assemble_open() got an unexpected keyword argument 'tape'`.

- [ ] **Step 3: Implement §2**

In `apps/worker/worker/assemble_open.py`, add the import and the builder, and change the signature:

```python
from worker.premarket import (
    PremarketQuote,
    TapeQuote,
    clears_threshold,
    gap_cents,
    pre_pct,
    premarket_vol_mult,
    tape_change,
)
```

(Task 5 needs only `TapeQuote`/`tape_change`; Task 6 uses the rest. Import the
full set once here so the module has one import block rather than two.)

```python
_OMITTED_TAPE = "No overnight tape captured for this session."
```

(replacing the M14 string — the feed exists now; the note only fires when the capture is missing.)

```python
def _overnight_tape(tape: list[TapeQuote]) -> dict[str, object]:
    """§2 Overnight tape — the macro backdrop plus the foreign proxies this
    book's sectors make relevant (docs/05). The narrated "read" paragraph lands
    in ``note`` at stage ⑤; assembly leaves it None."""
    rows = []
    for quote in tape:
        pct, absolute = tape_change(quote)
        rows.append({
            "symbol": quote.symbol,
            "label": quote.label,
            "level": _float(quote.last),
            "overnight_pct": _float(pct),
            "overnight_abs": _float(absolute),
        })
    return {
        "id": "overnight_tape",
        "tier": "full" if rows else "suppressed",
        "note": None if rows else _OMITTED_TAPE,
        "rows": rows,
    }
```

Add `tape: list[TapeQuote]` as a keyword parameter of `assemble_open` and replace `_omitted("overnight_tape", _OMITTED_TAPE)` in the `sections` list with `_overnight_tape(tape)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_assemble_open.py -v`
Expected: PASS (the pre-existing tests that call `assemble_open` positionally-by-keyword need `tape=[]`; the `_assemble` helper covers new ones — update the older calls to pass `tape=[]`, `premarket=[]`, `claims=[]`).

- [ ] **Step 5: Commit**

```bash
git add apps/worker/worker/assemble_open.py apps/worker/tests/test_assemble_open.py
git commit -m "feat(m15): §2 overnight tape — level, overnight change, book-relevant proxies"
```

---

### Task 6: §3 pre-market names, §5's pre-market column, §1 salience

**Files:**
- Modify: `apps/worker/worker/assemble_open.py`
- Modify: `apps/worker/tests/test_assemble_open.py`
- Modify: `apps/worker/tests/test_assemble_open_db.py`

**Interfaces:**
- Consumes: `premarket.PremarketQuote`, `pre_pct`, `gap_cents`, `premarket_vol_mult`, `clears_threshold` (Task 4).
- Produces: `assemble_open(..., premarket: list[PremarketQuote], ...)` emitting the `premarket` section (rows sorted by `|pre_pct|` descending, `why: None`), populating top-level `suppressed[]` with the names that did not clear, and filling `SectorSetup.premarket`. `SectorSetup` gains a `premarket: Decimal | None` field.

- [ ] **Step 1: Write the failing tests**

```python
from worker.premarket import PremarketQuote


def _pm(symbol: str, last: str, prev: str, v: int = 100_000, typical: str = "50000") -> PremarketQuote:
    return PremarketQuote(symbol, Decimal(last), v, Decimal(prev), Decimal(typical))


def test_premarket_lists_only_names_over_the_threshold() -> None:
    obj = _assemble(premarket=[
        _pm("SNDK", "49.26", "47.32"),   # +4.1%
        _pm("RKLB", "24.98", "25.00"),   # -0.1%
    ])
    section = _section(obj, "premarket")
    assert [r.symbol for r in section.rows] == ["SNDK"]


def test_a_row_carries_dollars_and_a_volume_multiple() -> None:
    row = _section(_assemble(premarket=[_pm("SNDK", "49.26", "47.32", v=150_000)]),
                   "premarket").rows[0]
    assert row.gap_cents == 194
    assert row.premarket_vol_mult == 3.0
    assert row.rvol is None  # never the 30-day RVOL (D3, docs/05)
    assert row.why is None   # narration fills it, stage ⑤


def test_names_under_the_threshold_roll_up() -> None:
    """The suppression principle (M5): the brief names what it skipped rather
    than pretending the book is two stocks."""
    obj = _assemble(premarket=[
        _pm("SNDK", "49.26", "47.32"),
        _pm("RKLB", "24.98", "25.00"),
        _pm("MU", "100.10", "100.00"),
    ])
    assert set(obj.suppressed) == {"RKLB", "MU"}
    assert _section(obj, "premarket").note is not None


def test_a_quiet_premarket_is_a_roll_up_line_not_an_empty_table() -> None:
    obj = _assemble(premarket=[_pm("RKLB", "24.98", "25.00")])
    section = _section(obj, "premarket")
    assert section.rows == []
    assert section.tier.value == "suppressed"
    assert "RKLB" in (section.note or "")


def test_the_biggest_gap_leads() -> None:
    """§1 salience: `one_thing` leads on the largest pre-market gap, so the row
    order is the salience order the narration prompt reads."""
    obj = _assemble(premarket=[
        _pm("AAOI", "18.30", "18.06"),   # +1.3%
        _pm("ASTS", "34.20", "36.08"),   # -5.2%
        _pm("SNDK", "49.26", "47.32"),   # +4.1%
    ])
    assert [r.symbol for r in _section(obj, "premarket").rows] == ["ASTS", "SNDK", "AAOI"]


def test_sector_setup_carries_the_premarket_column() -> None:
    sector = SectorSetup(
        sector_id="s1", name="Semis", benchmark_symbol="SMH",
        ret_5d=Decimal("0.02"), vs_spy_5d=Decimal("0.01"), premarket=Decimal("0.004"),
    )
    row = _section(_assemble(sectors=[sector]), "sector_setup").rows[0]
    assert row.premarket == 0.004
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_assemble_open.py -k "premarket or biggest" -v`
Expected: FAIL — unexpected keyword `premarket`; `SectorSetup.__init__() got an unexpected keyword argument 'premarket'`.

- [ ] **Step 3: Implement**

In `assemble_open.py`:

```python
_QUIET_PREMARKET = "Nothing moved pre-market."
# Replaces M14's "lands with the delayed-quote feed" — the feed exists now, so
# an empty §3 means no capture landed for this session, not a missing milestone.
_OMITTED_PREMARKET = "No pre-market quotes captured for this session."
```

Add `premarket: Decimal | None` to `SectorSetup` (after `vs_spy_5d`), and in `_sector_setup` replace `"premarket": None` with `"premarket": _float(s.premarket)`.

```python
def _premarket(quotes: list[PremarketQuote]) -> tuple[dict[str, object], list[str]]:
    """§3 Your names, pre-market. Returns the section and the names it skipped.

    Ordered by the size of the gap, largest first: §1 leads on the biggest
    pre-market move, and the ordering here is what makes that the row the
    narration prompt sees first (the M13 seam swaps this key for the largest
    overnight |resid_z| without touching anything else).
    """
    shown = [q for q in quotes if clears_threshold(q)]
    kept = {q.symbol for q in shown}
    skipped = sorted(q.symbol for q in quotes if q.symbol not in kept)
    shown.sort(key=lambda q: abs(pre_pct(q) or Decimal(0)), reverse=True)

    rows = [
        {
            "symbol": q.symbol,
            "pre_pct": _float(pre_pct(q)),
            "gap_cents": gap_cents(q),
            "premarket_vol_mult": _float(premarket_vol_mult(q)),
            "why": None,  # narration, stage ⑤
        }
        for q in shown
    ]
    note = None
    if skipped:
        note = f"{_QUIET_PREMARKET} " if not rows else ""
        note += f"{', '.join(skipped)} unchanged."
    return (
        {
            "id": "premarket",
            "tier": "full" if rows else "suppressed",
            "note": note or (None if rows else _OMITTED_PREMARKET),
            "rows": rows,
        },
        skipped,
    )
```

In `assemble_open`, take `premarket: list[PremarketQuote]`, build the section before the payload, and use it:

```python
    premarket_section, skipped = _premarket(premarket)
```

replace `_omitted("premarket", _OMITTED_PREMARKET)` with `premarket_section`, and `"suppressed": []` with `"suppressed": skipped`.

Update the module docstring's "No claims" / §2–§3 paragraphs to describe what M15 changed.

- [ ] **Step 4: Wire the DB reads**

In `read_open_inputs`, read the quotes and attach the sector pre-market column:

```python
def read_open_inputs(
    conn: Connection, user_id: str, session_date: date, prior_session: date
) -> tuple[list[CalendarEvent], list[SectorSetup], dict[str, str],
           list[TapeQuote], list[PremarketQuote]]:
    """Every read the open brief needs, in one place."""
    from worker.premarket import read_premarket, read_tape

    holdings = {
        row["symbol"]: str(row["status"])
        for row in conn.execute(_READ_HOLDINGS, {"user_id": user_id}).mappings()
    }
    events = _read_events(conn, session_date)
    sectors = _read_sectors(conn, user_id, prior_session, session_date)
    benchmarks = [s.benchmark_symbol for s in sectors if s.benchmark_symbol]
    tape = read_tape(conn, session_date, benchmarks)
    premarket = read_premarket(conn, sorted(holdings), session_date)
    return events, sectors, holdings, tape, premarket
```

`_read_sectors` gains `session_date` and fills `premarket` from the benchmark's own quote:

```python
def _read_sectors(
    conn: Connection, user_id: str, prior_session: date, session_date: date
) -> list[SectorSetup]:
    from worker.constants import BENCHMARK_SYMBOL
    from worker.premarket import pre_pct, read_premarket

    spy_5d = trailing_return(
        _read_closes(conn, BENCHMARK_SYMBOL, prior_session), _SECTOR_WINDOW
    )
    rows = list(conn.execute(_READ_SECTORS, {"user_id": user_id}).mappings())
    benchmarks = [r["benchmark_symbol"] for r in rows if r["benchmark_symbol"]]
    pre = {q.symbol: pre_pct(q) for q in read_premarket(conn, benchmarks, session_date)}

    out: list[SectorSetup] = []
    for row in rows:
        benchmark = row["benchmark_symbol"]
        ret_5d = (
            trailing_return(_read_closes(conn, benchmark, prior_session), _SECTOR_WINDOW)
            if benchmark
            else None
        )
        out.append(
            SectorSetup(
                sector_id=str(row["id"]),
                name=str(row["name"]),
                benchmark_symbol=benchmark,
                ret_5d=ret_5d,
                vs_spy_5d=(
                    ret_5d - spy_5d if ret_5d is not None and spy_5d is not None else None
                ),
                premarket=pre.get(benchmark),
            )
        )
    return out
```

Note the sector benchmarks must be captured pre-market for this column to fill — Task 10's ingest stage passes them in the `held` list alongside the holdings.

Update `assemble_open_and_store` to unpack the five-tuple and pass `tape=`/`premarket=`.

- [ ] **Step 5: Run everything**

Run: `uv run pytest tests/test_assemble_open.py tests/test_assemble_open_db.py -v`
Expected: PASS. Add a DB test asserting a seeded gapping name appears in §3 with a dollar gap while a flat one lands in `suppressed[]` (DoD 1).

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/assemble_open.py apps/worker/tests
git commit -m "feat(m15): §3 pre-market names, the §5 pre-market column, gap salience"
```

---

### Task 7: Narration — the §2 read and §3's `why` lines

**Files:**
- Modify: `apps/worker/worker/narrate.py`
- Modify: `apps/worker/tests/test_narrate.py`
- Modify: `docs/04-brief-object.md` (narration contract)

**Interfaces:**
- Consumes: the §2/§3 sections (Tasks 5–6).
- Produces: `_Narration` gains `tape_read: str | None`; `apply_narration` writes `why` into `premarket` rows as well as `attribution` rows, and `tape_read` into the `overnight_tape` section's `note`.

- [ ] **Step 1: Write the failing tests**

```python
def test_tape_read_lands_in_the_overnight_section() -> None:
    obj = _open_object_with_tape()   # helper: one overnight_tape row
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"tape_read": "Risk-off overnight; your semis proxy is soft."})
    )
    section = next(s for s in narrated.sections if s.id.value == "overnight_tape")
    assert section.note == "Risk-off overnight; your semis proxy is soft."


def test_a_tape_read_with_a_digit_is_dropped() -> None:
    """Invariant 2, unchanged: the tables carry every figure."""
    obj = _open_object_with_tape()
    narrated = narrate_open_and_apply(obj, lambda _p: json.dumps({"tape_read": "ES fell 0.4%."}))
    section = next(s for s in narrated.sections if s.id.value == "overnight_tape")
    assert section.note == next(
        s for s in obj.sections if s.id.value == "overnight_tape"
    ).note


def test_why_fills_premarket_rows() -> None:
    obj = _open_object_with_premarket("SNDK")
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"why": {"SNDK": "Memory pricing read-through."}})
    )
    row = next(s for s in narrated.sections if s.id.value == "premarket").rows[0]
    assert row.why == "Memory pricing read-through."


def test_a_why_for_an_unlisted_symbol_is_dropped() -> None:
    obj = _open_object_with_premarket("SNDK")
    narrated = narrate_open_and_apply(
        obj, lambda _p: json.dumps({"why": {"NVDA": "Not in this brief."}})
    )
    section = next(s for s in narrated.sections if s.id.value == "premarket")
    assert all(r.why is None for r in section.rows)


def test_narration_stays_non_fatal_for_the_open_brief() -> None:
    """D19 parity: the always-sending brief keeps sending."""
    obj = _open_object_with_tape()

    def boom(_p: str) -> str:
        raise RuntimeError("no key")

    assert narrate_open_and_apply(obj, boom) == obj
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_narrate.py -k "tape_read or premarket" -v`
Expected: FAIL — the note stays `None`, `why` stays `None`.

- [ ] **Step 3: Implement**

In `narrate.py`:

```python
class _Narration(BaseModel):
    one_thing: str | None = None
    why: dict[str, str] = {}
    # §2's "read" paragraph (M15). It lands in the overnight_tape section's
    # `note`, which is why it needs no schema change — the section already
    # carries a note, and the digit guard applies to it like everything else.
    tape_read: str | None = None
```

Replace `_attribution_symbols` with a narratable-symbol helper and widen `apply_narration`:

```python
_WHY_SECTIONS = (SectionId.attribution, SectionId.premarket)


def _narratable_symbols(obj: BriefObject) -> set[str]:
    """Symbols narration may key a `why` to: the close brief's attribution rows
    and the open brief's pre-market rows. A brief has one or the other."""
    out: set[str] = set()
    for section in obj.sections:
        if section.id in _WHY_SECTIONS:
            out |= {row.symbol for row in section.rows if row.symbol is not None}
    return out
```

In `parse_narration`, swap `_attribution_symbols(obj)` for `_narratable_symbols(obj)` and add the tape guard:

```python
    tape_read = narration.tape_read
    if tape_read is not None and _HAS_DIGIT.search(tape_read):
        tape_read = None
    return _Narration(one_thing=one_thing, why=why, tape_read=tape_read)
```

In `apply_narration`:

```python
    for section in payload["sections"]:
        if section["id"] in {s.value for s in _WHY_SECTIONS}:
            for row in section["rows"]:
                prose = narration.why.get(row["symbol"])
                if prose is not None:
                    row["why"] = prose
        if section["id"] == SectionId.overnight_tape.value and narration.tape_read:
            section["note"] = narration.tape_read
```

Extend `build_open_prompt` to ask for the two new keys:

```python
    symbols = sorted(_narratable_symbols(obj))
    ...
        '  "tape_read": one short paragraph reading the overnight tape against '
        "this book's sectors — what the macro backdrop implies for how these "
        "names open. Direction and cause in words; no figures.\n"
        f'  "why": an object mapping each of these tickers exactly — {symbols} — '
        "to one causal sentence explaining its pre-market move.\n"
        "Lead `one_thing` with the first pre-market row: it is the largest gap "
        "in the book this morning.\n"
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_narrate.py -v`
Expected: PASS, including the untouched close-brief narration tests.

- [ ] **Step 5: Document the contract**

In `docs/04-brief-object.md`, extend the narration contract block:

```jsonc
{
  "one_thing": "…",
  "tape_read": "…",          // open brief §2, lands in the section's note (M15)
  "why": { "SNDK": "…" },    // close: attribution rows · open: pre-market rows
  "sector_notes": { "semis": "…" }
}
```

and note under the three rules: "`tape_read` is subject to the same digit guard; a read containing a figure is dropped and §2 renders as a table alone."

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/narrate.py apps/worker/tests/test_narrate.py docs/04-brief-object.md
git commit -m "feat(m15): narration writes the §2 read and §3's why lines"
```

---

### Task 8: The horizon-0 morning claim

**Files:**
- Modify: `apps/worker/worker/claims.py`
- Modify: `apps/worker/worker/assemble_open.py`
- Create: `apps/worker/tests/test_claims_horizon0.py`

**Interfaces:**
- Consumes: `PremarketQuote`, `pre_pct`, `clears_threshold` (Task 4); `assemble_shared.claim_dict`.
- Produces:
  - `claims.emit_premarket_gap(quotes: list[PremarketQuote]) -> list[Claim]` — pure, `claim_type="premarket_gap"`, `horizon_sessions=0`, `direction` from the gap's sign.
  - `claims.resolve_due_claims` resolving horizon-0 claims emitted the *same* session.
  - `assemble_open(..., claims=...)` carrying them, and `assemble_open_and_store` persisting them via `store_emitted_claims`.

This is the one shared-with-the-close-brief change in M15. The horizon-1 path must come out byte-identical: `test_claims.py` and `test_claims_db.py` stay unmodified and green.

- [ ] **Step 1: Write the failing tests**

```python
# apps/worker/tests/test_claims_horizon0.py
"""The morning claim (M15): the open brief commits to a direction before the
bell and the *same session's* close brief grades it.

This is the loop D16b built the engine for and could not exercise — until now
every claim was horizon 1, resolved by the next day's close brief. Horizon 0 is
a real change to resolution, not a free ride on the claim_type seam:
`resolve_due_claims` read only `session_date < :session_date`, and
`_resolve_session` offset by `horizon - 1`, which is -1 at horizon 0.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from worker.claims import emit_premarket_gap
from worker.premarket import PremarketQuote


def _q(symbol: str, last: str, prev: str) -> PremarketQuote:
    return PremarketQuote(symbol, Decimal(last), 1000, Decimal(prev), Decimal("500"))


def test_a_gap_up_claims_relative_strength_today() -> None:
    (claim,) = emit_premarket_gap([_q("SNDK", "49.26", "47.32")])
    assert claim.claim_type == "premarket_gap"
    assert claim.direction == "up"
    assert claim.horizon_sessions == 0


def test_a_gap_down_claims_the_other_way() -> None:
    (claim,) = emit_premarket_gap([_q("ASTS", "34.20", "36.08")])
    assert claim.direction == "down"


def test_a_name_under_the_threshold_makes_no_claim() -> None:
    """The claim rides §3's threshold: if the move isn't worth a row, it isn't
    worth a falsifiable call."""
    assert emit_premarket_gap([_q("RKLB", "24.98", "25.00")]) == []
```

And the DB half, in the same file:

```python
def test_the_same_session_close_resolves_the_morning_claim(db_conn) -> None:
    """DoD 4, end to end: emitted at 08:15, graded from that day's close."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fc"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h0@example.invalid')"), {"u": user}
    )
    for symbol, prev, close in (("ZGAP", "10.00", "11.00"), ("SPY", "100.00", "100.50")):
        for d, c in ((date(2098, 5, 5), prev), (session, close)):
            db_conn.execute(
                text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
                     "VALUES (:s, :d, :c, :c, :c, :c, 1000, :c)"),
                {"s": symbol, "d": d, "c": Decimal(c)},
            )

    store_emitted_claims(
        db_conn, user, f"{user}-{session}-open", session,
        [Claim("ZGAP", "premarket_gap", "up", 0)],
    )
    (resolved,) = resolve_due_claims(db_conn, user, session)
    assert resolved.symbol == "ZGAP"
    assert resolved.outcome == "correct"  # +10% vs SPY's +0.5%


def test_a_horizon_one_claim_is_not_resolved_on_its_own_session(db_conn) -> None:
    """The regression that matters: widening the due-claims query must not let
    the close brief grade the claim it emitted minutes earlier."""
    from sqlalchemy import text

    from worker.claims import Claim, resolve_due_claims, store_emitted_claims

    session = date(2098, 5, 6)
    user = "00000000-0000-0000-0000-0000000000fd"
    db_conn.execute(
        text("INSERT INTO users (id, email) VALUES (:u, 'h1@example.invalid')"), {"u": user}
    )
    db_conn.execute(
        text("INSERT INTO bars_daily (symbol, session_date, o, h, l, c, v, adj_c) "
             "VALUES ('ZH1', :d, 10, 10, 10, 10, 1000, 10)"),
        {"d": session},
    )
    store_emitted_claims(
        db_conn, user, "b", session, [Claim("ZH1", "relative_strength", "up", 1)]
    )
    assert resolve_due_claims(db_conn, user, session) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_claims_horizon0.py -v`
Expected: FAIL — `ImportError: cannot import name 'emit_premarket_gap'`.

- [ ] **Step 3: Implement emission**

In `claims.py`:

```python
def emit_premarket_gap(quotes: list[PremarketQuote]) -> list[Claim]:
    """The morning call (M15): a name gapping pre-market is claimed to hold that
    relative direction into the close, **horizon 0** — resolved by that same
    session's close brief. This is the same-day open→close loop D16b built the
    engine for and could not run until an open brief emitted.

    The threshold is §3's: if the move isn't worth a row, it isn't worth a
    falsifiable call.
    """
    from worker.premarket import clears_threshold, pre_pct

    out: list[Claim] = []
    for quote in quotes:
        if not clears_threshold(quote):
            continue
        pct = pre_pct(quote)
        if pct is None or pct == 0:
            continue
        out.append(
            Claim(
                symbol=quote.symbol,
                claim_type="premarket_gap",
                direction="up" if pct > 0 else "down",
                horizon_sessions=0,
            )
        )
    return out
```

- [ ] **Step 4: Implement horizon-0 resolution**

Two surgical edits. First, `_READ_UNRESOLVED` admits same-session claims:

```python
# `<=`, not `<`: a horizon-0 morning claim is due the session it was emitted
# on. Horizon-1 claims emitted today are still excluded — not by this filter but
# by `_resolve_session`, which places their resolution on the *next* session.
_READ_UNRESOLVED = text("""
    SELECT id, symbol, claim_type, direction, horizon_sessions, session_date
    FROM claims
    WHERE user_id = :user_id AND outcome IS NULL AND session_date <= :session_date
""")
```

Second, `_resolve_session` grows the horizon-0 case:

```python
# Horizon 0 resolves on the emit session itself — but only once that session has
# a bar. Before the close there is nothing to grade, so the claim waits.
_SAME_SESSION = text("""
    SELECT session_date FROM bars_daily
    WHERE symbol = :symbol AND session_date = :emitted_on
""")


def _resolve_session(
    conn: Connection, symbol: str, emitted_on: date, horizon: int
) -> date | None:
    if horizon == 0:
        return conn.execute(
            _SAME_SESSION, {"symbol": symbol, "emitted_on": emitted_on}
        ).scalar()
    return conn.execute(
        _RESOLVE_SESSION, {"symbol": symbol, "emitted_on": emitted_on, "offset": horizon - 1}
    ).scalar()
```

`_grade` needs no change: a `premarket_gap` claim is a relative-strength call, graded from the same tape.

- [ ] **Step 5: Wire it into the open brief**

In `assemble_open`, take `claims: list[Claim]` and emit `"claims": [claim_dict(c, user_id, session_date, "open") for c in claims]` (import `claim_dict` from `assemble_shared`). In `assemble_open_and_store`, after assembly and before `_store`:

```python
    emitted = emit_premarket_gap(premarket)
```

pass `claims=emitted` into `assemble_open`, and after `_store(conn, obj)`:

```python
    store_emitted_claims(conn, user_id, obj.brief_id, session_date, emitted)
```

Leave `resolved_claims` empty and never call `resolve_due_claims` here — resolving at 08:15 would consume the due claims before the close brief's §7 could report them (the M14 note, now load-bearing).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, with `test_claims.py` / `test_claims_db.py` unmodified.

- [ ] **Step 7: Commit**

```bash
git add apps/worker/worker/claims.py apps/worker/worker/assemble_open.py apps/worker/tests/test_claims_horizon0.py
git commit -m "feat(m15): the horizon-0 morning claim the same session's close resolves"
```

---

### Task 9: Renderers — email template and web archive

**Files:**
- Modify: `apps/web/emails/open-brief.tsx`
- Modify: `apps/web/emails/open-brief-preview.tsx`
- Modify: `apps/web/app/briefs/[slug]/page.tsx`

**Interfaces:**
- Consumes: the v4 `Row` type (Task 2) and the §2/§3 sections (Tasks 5–7).
- Produces: rendered §2 and §3 in both surfaces. `design/design-reference.html`'s open-email tab (lines 495–534) is the visual authority.

- [ ] **Step 1: Update the preview fixture**

In `apps/web/emails/open-brief-preview.tsx`, add an `overnight_tape` section (two or three rows plus a `note` read) and a `premarket` section (three rows with `why`, `pre_pct`, `gap_cents`, `premarket_vol_mult`, plus a roll-up `note`), and set `schema_version: 4`. Mirror the design reference's figures so the preview is comparable to it.

- [ ] **Step 2: Render §2 in the email**

In `open-brief.tsx`, after the one-thing block:

```tsx
          {/* overnight tape */}
          {tape && tape.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Overnight tape" note="vs prior close" />
              <Tape rows={tape.rows} />
              {tape.note && <p style={note}>{tape.note}</p>}
            </Sec>
          )}
```

with `const tape = section("overnight_tape");` beside the existing lookups, and the sub-component:

```tsx
// Two columns of pairs, as in the design reference: six macro lines read faster
// side by side than as a six-row list.
function Tape({ rows }: { rows: Row[] }) {
  const pairs: Row[][] = [];
  for (let i = 0; i < rows.length; i += 2) pairs.push(rows.slice(i, i + 2));
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <tbody>
        {pairs.map((pair) => (
          <tr key={pair.map((r) => r.symbol).join("-")}>
            {pair.map((r) => (
              <React.Fragment key={r.symbol}>
                <td style={{ ...tdL, width: "28%" }}>
                  <span style={sym}>{r.label}</span>
                </td>
                <td style={{ ...tdR, width: "22%", color: signColor(r.overnight_pct ?? r.overnight_abs) }}>
                  {r.overnight_pct != null
                    ? pctOrDash(r.overnight_pct)
                    : `${fmtLevel(r.level)} ${signedAbs(r.overnight_abs)}`}
                </td>
              </React.Fragment>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmtLevel(level: number | null | undefined): string {
  return level == null ? "—" : level.toFixed(2);
}
function signedAbs(v: number | null | undefined): string {
  return v == null ? "" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}
```

(Add `import React from "react";` if the file does not already import it for `Fragment`.)

- [ ] **Step 3: Render §3 in the email**

```tsx
          {/* your names, pre-market */}
          {pre && pre.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Your names, pre-market" note="vs prior close" />
              <Premarket rows={pre.rows} />
              {pre.note && <p style={note}>{pre.note}</p>}
            </Sec>
          )}
          {pre && pre.rows.length === 0 && pre.note && (
            <Sec style={sec}>
              <SectionHead title="Your names, pre-market" />
              <p style={note}>{pre.note}</p>
            </Sec>
          )}
```

```tsx
function Premarket({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={{ ...thR, width: 62 }}>Pre</th>
          <th style={{ ...thR, width: 70 }}>Gap</th>
          <th style={{ ...thR, width: 66 }}>Pre vol</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              {r.why && <span style={why}>{r.why}</span>}
            </td>
            <td style={{ ...tdR, color: signColor(r.pre_pct) }}>{pctOrDash(r.pre_pct)}</td>
            <td style={{ ...tdR, color: signColor(r.gap_cents) }}>{dollarsOrDash(r.gap_cents)}</td>
            <td style={tdR}>{multOrDash(r.premarket_vol_mult)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// The gap is dollars, not percent — that's the figure you act on (docs/01).
function dollarsOrDash(cents: number | null | undefined): string {
  if (cents == null) return "—";
  const sign = cents >= 0 ? "+" : "−";
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`;
}
function multOrDash(m: number | null | undefined): string {
  return m == null ? "—" : `${m.toFixed(1)}×`;
}
```

Also drop `overnight_tape` / `premarket` from the `omitted` list when they carry rows — the existing filter already requires `tier === "suppressed" && note`, and a populated §3 with a roll-up note is `full`, so a quiet §3 (suppressed + note) would print its roll-up twice. Change the `omitted` filter to exclude sections the template renders itself:

```tsx
  const omitted = [tape, pre]
    .filter((s): s is Section => s?.tier === "suppressed" && !!s.note && s.rows.length === 0)
    .filter((s) => s.id !== "premarket")   // §3 renders its own roll-up above
    .map((s) => s.note);
```

- [ ] **Step 4: Mirror both sections in the web archive**

In `apps/web/app/briefs/[slug]/page.tsx`, add the same two sections above the existing calendar card, using that file's `S.card` / `S.tdR` conventions and the same formatters. The web archive is the permanent copy (docs/05's editorial principle), so it renders both sections whether or not they cleared a threshold.

- [ ] **Step 5: Verify**

Run: `pnpm --filter web typecheck && pnpm --filter web email:size` (the `check-size.ts` script)
Expected: typecheck clean; the open email stays under 80KB with a plaintext part (docs/06).

Then render the preview and compare against `design/design-reference.html`'s open tab side by side.

- [ ] **Step 6: Commit**

```bash
git add apps/web
git commit -m "feat(m15): render the overnight tape and pre-market names"
```

---

### Task 10: The pre-open ingest stage

**Files:**
- Modify: `apps/worker/worker/scheduler.py:280-348`
- Modify: `apps/worker/worker/cli.py`
- Modify: `apps/worker/tests/test_scheduler_open.py`

**Interfaces:**
- Consumes: `premarket.ingest_premarket`, `prior_closes`, `capture_stamp`, `tape_universe` (Task 4); `SyntheticPremarketProvider` (Task 3).
- Produces: `scheduler.ingest_premarket_for_session(engine, session_date, prior_session, user_id, provider=None) -> int`, called by `run_open_session_job` before assembly; CLI `seed-premarket --date`.

docs/02 stages the morning as ingest 08:00 → assemble 08:10 → send 08:15. M14's open job had no ingest work; this is the work.

- [ ] **Step 1: Write the failing test**

```python
class _Stop(Exception):
    """Cuts the job short after the step under test — the ordering is the
    assertion, and delivery is somebody else's test."""


def test_the_open_job_captures_premarket_before_assembling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/02's staging, made real: the 08:15 send reads quotes the 08:00 stage
    wrote. M14's open job had nothing to ingest; M15 gives it the morning's."""
    from worker import scheduler

    calls: list[str] = []

    def fake_ingest(*_args: object, **_kwargs: object) -> int:
        calls.append("ingest")
        return 3

    def fake_assemble(*_args: object, **_kwargs: object) -> object:
        calls.append("assemble")
        raise _Stop

    monkeypatch.setattr(scheduler, "ingest_premarket_for_session", fake_ingest)
    monkeypatch.setattr(
        "worker.assemble_open.assemble_open_and_store", fake_assemble
    )

    with pytest.raises(_Stop):
        scheduler.run_open_session_job(
            _engine(),                                  # the file's existing stub engine
            now_utc=datetime(2026, 8, 13, 12, 15, tzinfo=UTC),
            healthcheck_url="",
        )
    assert calls == ["ingest", "assemble"]
```

Reuse `test_scheduler_open.py`'s existing engine stub and session-date constants rather than adding new ones; the file already monkeypatches `deliver_brief` the same way.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scheduler_open.py -v`
Expected: FAIL — `calls == ["assemble"]`; no ingest happens.

- [ ] **Step 3: Implement**

In `scheduler.py`:

```python
def ingest_premarket_for_session(
    engine: Engine,
    *,
    session_date: date,
    prior_session: date,
    user_id: str = DEV_USER_ID,
    provider: MarketDataProvider | None = None,
) -> int:
    """The 08:00 stage (docs/02): capture the morning's pre-market prints and the
    overnight macro tape into `quotes`.

    The provider defaults to the synthetic feed. That is not a test seam — it is
    the shipping configuration until the premium pre-market licence lands (D8),
    and swapping it is a one-line change here.
    """
    from worker.premarket import (
        capture_stamp, ingest_premarket, prior_closes, tape_universe,
    )
    from worker.providers.synthetic import SyntheticPremarketProvider

    with engine.connect() as conn:
        held = book_symbols(conn, user_id)
        benchmarks = [
            str(r[0])
            for r in conn.execute(
                text("SELECT DISTINCT benchmark_symbol FROM sectors "
                     "WHERE user_id = :u AND benchmark_symbol IS NOT NULL"),
                {"u": user_id},
            ).all()
        ]
        tape = tape_universe(benchmarks)
        closes = prior_closes(
            conn, held + [symbol for symbol, _, _ in tape], prior_session
        )

    # The tape symbols have no bars (nothing ingests futures), so seed their
    # bases from the tape's own prior capture where one exists, and skip the
    # rest — a symbol with no base is omitted rather than invented.
    with engine.connect() as conn:
        closes |= _prior_tape_levels(conn, [s for s, _, _ in tape], prior_session)

    prov = provider or SyntheticPremarketProvider(closes, session_date)
    with engine.begin() as conn:
        return ingest_premarket(
            conn, prov,
            held=held, tape=tape,
            session_date=session_date,
            captured_at=capture_stamp(session_date),
        )


_PRIOR_TAPE = text("""
    SELECT symbol, last FROM quotes
    WHERE session_date = :prior_session AND symbol = ANY(:symbols)
      AND last IS NOT NULL
""")


def _prior_tape_levels(
    conn: Connection, symbols: list[str], prior_session: date
) -> dict[str, Decimal]:
    """Yesterday's capture is today's base for the macro tape — nothing ingests
    futures or yield bars, so the tape bootstraps off its own history. On the
    first morning it is empty and §2 renders its note."""
    return {
        str(row["symbol"]): Decimal(str(row["last"]))
        for row in conn.execute(
            _PRIOR_TAPE, {"prior_session": prior_session, "symbols": symbols}
        ).mappings()
    }
```

(`from decimal import Decimal` at the top of `scheduler.py`.)

Then, in `run_open_session_job`, immediately after `prior = calendar.previous_session(session_date)`:

```python
        written = ingest_premarket_for_session(
            engine, session_date=session_date, prior_session=prior, user_id=user_id
        )
        print(f"open {session_date}: captured {written} pre-market quotes.")
```

Update the docstring, which currently says the open job has no ingest work.

- [ ] **Step 4: Add a bootstrap CLI command**

In `cli.py`, add `seed-premarket --date` calling `ingest_premarket_for_session` so a tape can be seeded for a run of sessions before the first real morning (the volume multiple needs `PREMARKET_VOL_MIN_OBS` prior captures, and §2's tape needs a prior level). Follow the `seed-events` command's shape.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_scheduler_open.py -v && uv run -m worker.cli schedule --dry-run`
Expected: tests PASS; the dry-run prints both fire kinds unchanged (DoD parity with M14's item 5 — the ingest stage must not move a fire time).

- [ ] **Step 6: Commit**

```bash
git add apps/worker/worker/scheduler.py apps/worker/worker/cli.py apps/worker/tests/test_scheduler_open.py
git commit -m "feat(m15): the 08:00 pre-market capture stage the open job was missing"
```

---

### Task 11: Snapshot, docs, and the milestone tick

**Files:**
- Modify: `apps/worker/tests/fixtures/open_brief.json` (regenerated)
- Modify: `apps/worker/tests/test_assemble_open.py`
- Modify: `docs/07-decisions.md`, `docs/08-milestones.md`, `docs/superpowers/specs/2026-08-13-m15-open-brief-premarket-design.md`

- [ ] **Step 1: Regenerate the open snapshot with §2/§3 populated**

Extend the snapshot test's inputs to include two tape quotes, one gapping name and one flat one, then delete `tests/fixtures/open_brief.json` and re-run so it regenerates. Inspect the diff: `schema_version` 4, an `overnight_tape` section with rows, a `premarket` section with one row and a roll-up note, a populated `suppressed[]`, and one `premarket_gap` claim at horizon 0.

Run: `uv run pytest tests/test_assemble_open.py -v`

- [ ] **Step 2: Add the decision entry**

Append to `docs/07-decisions.md`:

```markdown
---

**D24 — M15 pre-market: a real `quotes` table, a deterministic synthetic feed, and horizon-0 as an engine change**
§2 (overnight tape) and §3 (pre-market names) run on a **new `quotes` table** —
migration `0011_quotes`. The design spec said the feeds needed no migration
because `quotes` already existed; it never did (`docs/03` sketched it, `0003`
created only `raw_payloads`/`bars_daily`), the same shape of error M14 found with
`events`. It is keyed **`(symbol, session_date)`** rather than the sketch's
`(symbol, captured_at)`: every read is "the capture for session D", and the
session key makes re-seeding idempotent. Shared, no `user_id` (D18/D21).
Data comes from `SyntheticPremarketProvider` — deterministic by hash over
`(symbol, session_date)`, never `random`, so a seeded morning is
snapshot-testable — behind the same four `MarketDataProvider` methods a licensed
fdnpy premium feed will implement (`get_latest_prices` / `get_futures_prices` /
`get_index_quotes` / `get_forex_quotes`). One ingest function serves both, which
is what makes the swap a constructor change (D8 stays a business call).
§3's volume multiple is **pre-market-specific** — this morning's `extended_v`
against the mean of the prior sessions' captures — never the 30-day RVOL, which
over a pre-market tape is a number that looks meaningful and isn't (D3).
The `>1% **or carrying news**` threshold ships half-wired: `clears_threshold`
takes `has_news` and nothing sets it, because there is no news feed (docs/02:
Premium, unwired) — the D18 `short_interest` precedent.
The **horizon-0 morning claim** is a genuine change to the D17 engine, not a
`claim_type` seam ride: the contract pinned `horizon_sessions` to `minimum: 1`,
`resolve_due_claims` read only `session_date < :session_date`, and
`_resolve_session` offset by `horizon - 1`. Resolution now admits same-session
claims and resolves horizon 0 on the emit session's own bar; horizon-1 claims are
still excluded from their own session by `_resolve_session`, which is asserted by
a regression test. The open brief emits and **never resolves** — resolving at
08:15 would consume the due claims before the close brief's §7 could report them.
`schema_version` bumps to **4** (§2 `level`/`overnight_pct`/`overnight_abs`, §3
`pre_pct`/`gap_cents`/`premarket_vol_mult`, the claim changes); narration gains a
`tape_read` key that lands in §2's existing `note`, so the read paragraph costs
no schema surface and inherits the digit guard.
*Rules out:* a `quotes` table keyed by capture timestamp; RVOL over pre-market
volume; the open brief resolving claims; a section that knows which provider
filled `quotes`; blocking §2/§3 on the data licence.
*Reverses if:* the premium pre-market licence lands (swap the provider, delete
nothing else); a news feed lands (`has_news` starts firing with no other change);
or M13's attribution lands and §1's salience upgrades from the largest gap to the
largest overnight `|resid_z|` — a change to one sort key in `_premarket`.
```

- [ ] **Step 3: Tick the milestone**

In `docs/08-milestones.md`, change M15's `- [ ]` to `- [x]` and append to its line: *"Built on `0011_quotes` and a deterministic synthetic feed behind the provider seam; the live premium swap is a constructor change (D24)."*

- [ ] **Step 4: Correct the spec**

In the M15 design spec, fix the four claims listed in this plan's "Spec corrections" section — most importantly replacing "No new tables; no Alembic migration for the feeds" with the `0011_quotes` reality, and noting that horizon 0 required an engine change.

- [ ] **Step 5: Full verification**

Run, from `session-brief/`:

```bash
pnpm contracts:gen && git diff --exit-code packages/contracts apps/worker/contracts apps/web/lib/contracts
cd apps/worker && uv run pytest -q && uv run ruff check . && uv run mypy --strict worker
cd ../.. && pnpm --filter web typecheck && pnpm --filter web lint
```

Expected: contracts clean, full suite green, lint and types clean.

- [ ] **Step 6: Commit**

```bash
git add docs apps/worker/tests
git commit -m "docs(m15): D24, the milestone tick, and four spec corrections"
```

---

## Definition of Done → task map

| Spec DoD | Where it is proven |
|---|---|
| 1. §3 threshold — gapper appears with pre %, dollars, volume multiple; flat name omitted; quiet pre-market rolls up | Task 6 (pure) + Task 6 step 5 (DB) |
| 2. Not RVOL | Task 4 (`test_volume_multiple_is_against_typical_premarket_volume`) + Task 6 (`row.rvol is None`) |
| 3. §2 read — per-symbol overnight change, narrated read, proxies from the book's sectors | Task 5 + Task 7 + `tape_universe` (Task 4) |
| 4. Morning claim resolved by the same session's close | Task 8 (`test_the_same_session_close_resolves_the_morning_claim`) |
| 5. Provider swap — seed and mock-live produce the same shape | Task 4 (`test_a_live_provider_takes_the_same_path`) |
| 6. Back-compat — M14-era brief still renders; contracts clean | Task 2 (`open_brief_v3.json`, `close_brief_v2.json`) + Task 11 step 5 |

## Out of scope (per the spec)

Real-time data and any intraday send (D9 stands) · securing the data licence (D8, a business decision) · the M13 attribution-residual salience upgrade for §1 · options/anatomy triggers · a live news feed for §3's `has_news` clause.
