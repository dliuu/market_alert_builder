# M26 Premium §6: every held name, calls in dollars — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the close brief's §6 from a three-name gate into a ledger of every held name, with IV30 and its change, the VIX comparison, and each selected call priced in dollars with its change since the prior session.

**Architecture:** The worker's `VolReading` and `StripRow` gain the three missing numbers (`iv30_chg`, `book_median_ratio`, `mid_chg`); the JSON Schema gains a nested `call_quote` and three row fields at schema 11; `_premium` stops gating and emits a `calls` array per name; both renderers read `calls` and fall back to the v10 flat fields. No migration, no new ingest.

**Tech Stack:** Python 3.12 / Decimal / SQLAlchemy text queries / pytest; JSON Schema → `datamodel-codegen` + `json2ts`; React Email + Next.js 15 (TypeScript, biome).

**Spec:** `docs/superpowers/specs/2026-10-25-m26-premium-every-name-design.md`

## Global Constraints

- No action verb anywhere in §6: templates, prompt, notes, headers, fixtures. The guard list is `_ACTION_VERB` in `apps/worker/worker/narrate.py` (sell/write/buy/hold/overwrite/roll and inflections). "writable", "across N" are allowed.
- `packages/contracts/brief-object.schema.json` is the source of truth; never hand-edit `apps/worker/contracts/brief.py` or `apps/web/lib/contracts/brief.ts`; run `pnpm contracts:gen` after every schema edit.
- Money is integer cents in the object (`contract_cents`, `writable_cents`); prices (`mid`, `mid_chg`) are floats like `close`.
- Decimal in the worker; floats only at `_premium_row` via `_ratio`.
- Stale readings carry null for every `*_chg`.
- Close email under 80KB with twenty names in §6 (`pnpm --filter web email:check`).
- Stored v10 bodies render unchanged: renderers treat a null `calls` as absent and fall back to the flat `call_*` fields.
- D33: run DB tests and dry runs with `DATABASE_URL=postgresql+psycopg://localhost/session_brief_premium_v2` (already created, at head `0020_options`, with the `auth.uid()` shim). Never point at the shared Supabase database from this branch.
- Commit after each task; `uv run ruff check . && uv run mypy` in `apps/worker`, `pnpm --filter web typecheck && pnpm check` at the root, before each commit that touches that side.

## Review Focus

1. A name whose prior session was stale: `iv30_chg` measures against the carried IV30 stored under the stale date, and is non-null. Pinned in Task 1 (`test_iv30_chg_after_a_stale_session_reads_the_carried_value`).
2. A strip cell whose contract is new today (expiry rolled): `mid_chg` null, `premium_pct_chg` non-null. Pinned in Task 1 (`test_strip_rows_mid_chg_is_the_same_contract_dollar_change`).
3. A position with 0 writable contracts: row present, `writable_cents` null, `contract_cents` non-null. Pinned in Task 3.
4. A stored v10 body with `calls` absent and `call_atm7_pct` present: both renderers print the old strip line. Pinned in Task 5 (preview sample) and Task 6 (v10 fixture render in `check-size.ts`).
5. A §6 with twenty names must not clip in Gmail. Pinned in Task 5 (`close_brief_premium20.json` in `check-size.ts`).

---

### Task 1: Worker reading gains `iv30_chg`, `book_median_ratio`, `mid_chg`

**Files:**
- Modify: `apps/worker/worker/volatility.py` (`StripRow` ~256, `strip_rows` ~286, `VolReading` ~333, `compute_and_store_volatility` ~522, queries ~423-500, `_read_prior_strip` ~757)
- Test: `apps/worker/tests/test_volatility.py`, `apps/worker/tests/test_volatility_db.py`, `apps/worker/tests/test_assemble.py` (`_cell`, `_reading` helpers gain the new fields)

**Interfaces:**
- Produces: `StripRow.mid_chg: Decimal | None`; `VolReading.iv30_chg: Decimal | None`; `VolReading.book_median_ratio: Decimal | None`; `_read_prior_iv30(conn, user_id, symbols, session_date) -> dict[str, Decimal]` returning the `iv30` metric stored for exactly `session_date` (the caller passes `previous_session`).

- [ ] **Step 1: Write the failing unit test for `mid_chg`** in `tests/test_volatility.py` next to `test_strip_rows_computes_the_three_changes_against_the_priors`:

```python
def test_strip_rows_mid_chg_is_the_same_contract_dollar_change() -> None:
    spot = Decimal("100")
    cells = select_strip(_grid(SESSION + timedelta(days=30), spot), spot, SESSION)
    atm30 = next(c for c in cells if (c.tenor, c.rule) == (30, "atm"))
    assert atm30.contract is not None
    rows = strip_rows(cells, {}, {atm30.contract.symbol: Decimal("0.84")})
    row = next(r for r in rows if (r.tenor, r.rule) == (30, "atm"))
    assert row.mid_chg == Decimal("1.05") - Decimal("0.84")
    other = next(r for r in rows if (r.tenor, r.rule) == (30, "otm10"))
    assert other.mid_chg is None  # no prior quote for that contract
```

- [ ] **Step 2: Run it** — `cd apps/worker && uv run pytest tests/test_volatility.py -k mid_chg -v`. Expected: FAIL, `StripRow` has no `mid_chg`.

- [ ] **Step 3: Add `mid_chg: Decimal | None` to `StripRow`** (after `chg_contract_pct`), set it in `strip_rows` as `cell.mid − prior_mids[c.symbol]` when both exist, else `None`; `_read_prior_strip` passes `mid_chg=None`. Update `_cell` in `tests/test_assemble.py` and `_prior_row` in `tests/test_volatility.py` with `mid_chg=None`.

- [ ] **Step 4: Run the two files** — `uv run pytest tests/test_volatility.py tests/test_assemble.py -q`. Expected: PASS.

- [ ] **Step 5: Write the failing DB tests for `iv30_chg`** in `tests/test_volatility_db.py`:

```python
def test_iv30_chg_is_against_the_prior_sessions_stored_iv30(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    first, second = _run(db_conn)
    assert first["ZZVOL"].iv30_chg is None  # nothing stored before session 0
    r = second["ZZVOL"]
    assert r.iv30 is not None and first["ZZVOL"].iv30 is not None
    assert r.iv30_chg == r.iv30 - first["ZZVOL"].iv30
    assert r.book_median_ratio is not None


def test_a_stale_reading_carries_no_iv30_chg(db_conn: Connection) -> None:
    _pull(db_conn, _seed(db_conn))
    _, second = _run(db_conn)
    assert second[STALE].stale and second[STALE].iv30_chg is None


def test_iv30_chg_after_a_stale_session_reads_the_carried_value(db_conn: Connection) -> None:
    """Review Focus 1: session 1 stored STALE's carried IV30 under session 1's
    date, so a fresh chain on session 2 measures against that number."""
    closes = _seed(db_conn)
    _pull(db_conn, closes)
    _run(db_conn)
    third = next_session(SESSIONS[1])
    provider = SyntheticOptionsProvider({STALE: closes[SESSIONS[1]]}, third)
    assert pull_options(db_conn, provider, [STALE], third).symbols_failed == {}
    out = compute_and_store_volatility(db_conn, USER, [STALE], third)
    assert not out[STALE].stale and out[STALE].iv30_chg is not None
```

(`next_session` is in `worker.calendar`; if `third` has no bar seeded the reading still exists per the module docstring. If `SyntheticOptionsProvider` needs a bar for the close, seed one with `closes[SESSIONS[1]]` the way `_seed` does.)

- [ ] **Step 6: Run them** — `DATABASE_URL=postgresql+psycopg://localhost/session_brief_premium_v2 uv run pytest tests/test_volatility_db.py -k iv30_chg -v`. Expected: FAIL, `VolReading` has no `iv30_chg`.

- [ ] **Step 7: Implement.** Add `iv30_chg: Decimal | None` and `book_median_ratio: Decimal | None` to `VolReading` (after `iv30_pctile` and after `premium` respectively). Add `_READ_PRIOR_IV30` (`SELECT symbol, value FROM metrics WHERE user_id = :user_id AND symbol = ANY(:symbols) AND metric = 'iv30' AND session_date = :session_date`) and `_read_prior_iv30`. In `compute_and_store_volatility`: read it once for `prior`; per symbol `iv30_chg = iv30 − prior_iv30[symbol]` when not stale and both exist, else `None`; in the `replace` step pass `book_median_ratio=book_median_ratio`. Do **not** add `iv30_chg` to `_store`.

- [ ] **Step 8: Run the worker suite with the DB** — `DATABASE_URL=… uv run pytest -q && uv run ruff check . && uv run mypy`. Expected: all pass (the `METRIC_KEYS` set in the DB test must not include the new fields).

- [ ] **Step 9: Commit** — `git add apps/worker && git commit -m "feat(premium): iv30_chg, book_median_ratio and same-contract mid_chg on the reading"`.

---

### Task 2: Schema 11 — `call_quote`, `calls`, three row fields

**Files:**
- Modify: `packages/contracts/brief-object.schema.json` (`$defs.row` after `call_atm7_pct` ~407; new `$defs.call_quote` next to `flag` ~417)
- Modify: `apps/worker/worker/assemble.py:82` (`SCHEMA_VERSION = 11`)
- Regenerate: `apps/worker/contracts/brief.py`, `apps/web/lib/contracts/brief.ts`
- Test: `apps/worker/tests/test_assemble.py::test_schema_version_is_ten` → `test_schema_version_is_eleven`

**Interfaces:**
- Produces: Pydantic `CallQuote` model (codegen names it from the `$def` title; check the generated name and use it in Task 3) with fields `tenor`, `rule`, `contract`, `strike`, `expiry`, `dte`, `moneyness`, `mid`, `mid_chg`, `contract_cents`, `writable_cents`, `premium_pct`, `premium_pct_chg`, `time_value_pct`, `iv`, `iv_chg`, `delta`, `open_interest`, `target_miss`, `tenor_collapsed`; `Row.calls: list[CallQuote] | None`; `Row.iv30_chg`, `Row.vix_close`, `Row.iv_vix_book_median`: `float | None`. TS: `CallQuote` interface, `Row.calls?: CallQuote[] | null`.

- [ ] **Step 1: Rename the version test** to `test_schema_version_is_eleven`, asserting `== 11`, and extend its comment with `v11 = M26's ungated §6 with calls[]`. Run: `uv run pytest tests/test_assemble.py -k schema_version`. Expected: FAIL (10 ≠ 11).

- [ ] **Step 2: Edit the schema.** In `$defs.row.properties` add, after `call_atm7_pct` (whose description gains " v10; not emitted from v11, see `calls`." — same for the other four flat fields):
  - `iv30_chg`: `["number","null"]`, "Close brief §6: day-over-day change in `iv30`, vol points, against the prior session's stored IV30; null across any gap and on a stale reading (M26)."
  - `vix_close`: `["number","null"]`, "Close brief §6: the VIX close `iv_vix_ratio` and `vix_pctile` are measured against (M26)."
  - `iv_vix_book_median`: `["number","null"]`, "Close brief §6: the book's median IV/VIX ratio that session, the regime's no-baseline fallback base (M26)."
  - `calls`: `{"type": ["array","null"], "items": {"$ref": "#/$defs/call_quote"}, "description": "Close brief §6: the selected calls in fixed order — 30-day ATM, 30-day 10% OTM, 7-day ATM — each present even when no contract was quoted (M26)."}`
  Add `$defs.call_quote`: `additionalProperties: false`, `required: ["tenor","rule","target_miss","tenor_collapsed"]`, properties: `tenor` `{"enum":[7,14,21,30]}`, `rule` `{"enum":["itm10","atm","otm10"]}`, `contract` string|null, `strike` number|null, `expiry` string|null with `"format":"date"`, `dte` integer|null, `moneyness` number|null, `mid` number|null, `mid_chg` number|null, `contract_cents` integer|null, `writable_cents` integer|null, `premium_pct` number|null, `premium_pct_chg` number|null, `time_value_pct` number|null, `iv` number|null, `iv_chg` number|null, `delta` number|null, `open_interest` integer|null, `target_miss` boolean, `tenor_collapsed` boolean. Descriptions from the spec's `call_quote` table, one line each, ending "(M26)".

- [ ] **Step 3: Regenerate and bump** — `pnpm contracts:gen`; set `SCHEMA_VERSION = 11`. Inspect `apps/worker/contracts/brief.py` for the generated class name of `call_quote` and that `expiry` is typed `date | None`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_assemble.py tests/test_contract_schema.py -q`. Expected: `test_schema_version_is_eleven` passes; `test_matches_frozen_fixture` and `test_frozen_fixture_carries_a_tiered_premium_section` now FAIL on `schema_version` (expected; Task 3 refreezes). Nothing else fails.

- [ ] **Step 5: Commit** — `git add packages apps/worker/contracts apps/web/lib/contracts apps/worker/worker/assemble.py apps/worker/tests/test_assemble.py && git commit -m "feat(contracts): schema 11, call_quote and the §6 calls[] row fields"`.

---

### Task 3: Assembly — every name, regime orders, `calls[]`, cap 20

**Files:**
- Modify: `apps/worker/worker/assemble.py:519-611` (`_qualifies_premium` deleted, `_premium_rank`, `_premium`, `_premium_row`, new `_call_quote`), `apps/worker/worker/constants.py:175` (`PREMIUM_CAP = 20`)
- Test: `apps/worker/tests/test_assemble.py` (the `# --- M24 (v10)` block ~790-1011)
- Fixtures: `apps/worker/tests/fixtures/close_brief.json` (refrozen), new `close_brief_v11.json` (copy), `close_brief_v10.json` unchanged; `apps/worker/tests/test_contract_schema.py` list gains `close_brief_v11.json`

**Interfaces:**
- Consumes: Task 1 fields; Task 2 `CallQuote`.
- Produces: `_premium(positions, premium) -> dict` unchanged signature; `_premium_row(r: VolReading, tier: str) -> dict[str, object]`; `_call_quote(cell: StripRow | None, tenor: int, rule: str, contracts_writable: int) -> dict[str, object]`.

- [ ] **Step 1: Rewrite the M24 test block.** Update `_cell` to take `mid: str | None = None, mid_chg: str | None = None, iv: str | None = None, iv_chg: str | None = None, contract: str | None = None, strike`-bearing contract symbol such as `"A     261120C00150000"`, `dte`, `moneyness`, `open_interest`; `_reading` gains `iv30_chg=Decimal("3.2")`, `book_median_ratio=Decimal("1.9")`. Replace the five gating tests with:

```python
def test_every_present_name_is_full_and_regime_orders() -> None:
    obj = _assemble_premium({
        "A": _reading("A", premium="fair"),
        "B": _reading("B", iv30_pctile=Decimal("95")),
        "C": _reading("C", premium="cheap", iv30_pctile=Decimal("5"), earnings_date=date(2026, 8, 20)),
        "D": _reading("D", premium="fair", iv30_thin=True, contracts_writable=0),
    })
    section = _section(obj, "premium")
    assert [(r.symbol, _tier_of(r)) for r in section.rows] == [
        ("C", "full"), ("B", "full"), ("A", "full"), ("D", "full")]
    assert section.note is None
    c = section.rows[0]
    assert c.earnings_in_window is True  # a guard is a badge, not a gate
    d = section.rows[3]
    assert d.iv30_thin is True and d.contracts_writable == 0


def test_row_carries_the_iv_change_the_vix_and_the_calls() -> None:
    obj = _assemble_premium({"A": _reading("A")})
    row = _section(obj, "premium").rows[0]
    assert row.iv30 == 42.5 and row.iv30_chg == 3.2
    assert row.vix_close == 20.2 and row.iv_vix_book_median == 1.9
    assert row.call_atm30_pct is None and row.call_atm7_pct is None  # v10 fields not emitted
    assert row.calls is not None
    assert [(c.tenor, c.rule.value) for c in row.calls] == [(30, "atm"), (30, "otm10"), (7, "atm")]
    atm30 = row.calls[0]
    assert atm30.contract == "A     261120C00150000"
    assert atm30.strike == 150.0 and atm30.expiry is not None and atm30.expiry.isoformat() == "2026-11-20"
    assert atm30.mid == 4.8 and atm30.mid_chg == 0.45
    assert atm30.contract_cents == 48000 and atm30.writable_cents == 96000  # 2 writable
    assert atm30.premium_pct == 0.048 and atm30.premium_pct_chg == 0.02
    assert atm30.iv == 41.0 and atm30.iv_chg == 2.1 and atm30.delta == 0.52


def test_zero_writable_nulls_the_across_sum_and_an_empty_cell_keeps_its_slot() -> None:
    obj = _assemble_premium({"A": _reading("A", contracts_writable=0, strip=(_cell(30, "atm", "0.048", "0.02", mid="4.80"),))})
    row = _section(obj, "premium").rows[0]
    assert row.calls is not None and len(row.calls) == 3
    assert row.calls[0].contract_cents == 48000 and row.calls[0].writable_cents is None
    assert row.calls[1].contract is None and row.calls[1].mid is None and row.calls[1].target_miss is False


def test_a_null_percentile_still_shows_iv30_and_sorts_after_real_ones() -> None:
    obj = _assemble_premium({
        "A": _reading("A", iv30_pctile=None),
        "B": _reading("B", iv30_pctile=Decimal("80")),
    })
    rows = _section(obj, "premium").rows
    assert [r.symbol for r in rows] == ["B", "A"] and rows[1].iv30 == 42.5


def test_twenty_first_name_is_brief_and_named_in_the_note() -> None:
    readings = {f"S{i:02d}": _reading(f"S{i:02d}", premium="fair") for i in range(21)}
    section = _section(_assemble_premium(readings), "premium")
    tiers = [_tier_of(r) for r in section.rows]
    assert tiers.count("full") == 20 and tiers[-1] == "brief"
    assert section.note == "S20 also measured — see the archive"


def test_held_names_without_a_reading_are_named_in_the_note() -> None:
    # `_assemble_premium` builds lots for every key; pass a reading for A only,
    # with lots for A and B (extend the helper with an optional `held` list).
    obj = _assemble_premium({"A": _reading("A")}, held=["A", "B"])
    assert _section(obj, "premium").note == "No option data for B"
```

Keep `test_cheap_extreme_outranks_a_milder_rich_name` and `test_null_percentile_ranks_last_then_wider_spread_leads`, rewritten to assert order only (all rows are `full`, note `None`). Delete `test_fourth_qualifier_is_brief_and_named_in_the_note`, `test_thin_fair_and_unwritable_names_never_qualify`, `test_earnings_in_window_demotes_to_brief_and_notes_no_qualifier`, the 7-day-slope test (`for slope in (0, -2, None)…`). Overflow note wording: `"{names} also measured — see the archive"` (the old "also rich or cheap" no longer holds).

- [ ] **Step 2: Run** — `uv run pytest tests/test_assemble.py -q -k "premium or calls or writable or note"`. Expected: FAIL on the new assertions.

- [ ] **Step 3: Implement.** `PREMIUM_CAP = 20`. Delete `_qualifies_premium`. `_premium_rank` becomes `(regime_is_fair, pctile_is_none, -distance, -|spread|, symbol)`. `_premium`: `present` as today; `ordered = sorted(present, key=_premium_rank)`; `shown, overflow = ordered[:PREMIUM_CAP], ordered[PREMIUM_CAP:]`; missing = held symbols not in `premium`; note: no present → `"No option data for the held names"`; missing → `"No option data for " + ", ".join(sorted(missing))`; overflow → `"{names} also measured — see the archive"` (missing and overflow may both apply: join with `"; "`, missing first); else `None`. `_premium_row` drops the five flat keys and adds `iv30_chg`, `vix_close`, `iv_vix_book_median` via `_ratio`, and `calls = [_call_quote(cells.get(k), *k, r.contracts_writable) for k in ((30,"atm"),(30,"otm10"),(7,"atm"))]`. `_call_quote`: parse `strike`/`expiry` from `cell.contract` with `worker.options.parse_occ`; `contract_cents = int((mid * 10000).to_integral_value(ROUND_HALF_UP))`; `writable_cents = contract_cents * n` or `None` when `n == 0` or `contract_cents is None`; an absent cell yields every quote field `None` and both booleans `False`.

- [ ] **Step 4: Run the file** — `uv run pytest tests/test_assemble.py -q`. Expected: only the two frozen-fixture tests fail.

- [ ] **Step 5: Refreeze.** Update `_mixed()` so its four readings carry contracts and mids (`_cell(..., mid=…, contract=…)`), then write the new body: `uv run python -c "import json,sys; sys.path.insert(0,'tests'); from test_assemble import _mixed, config; config.OPTIONS_LIVE=False; from worker.assemble_shared import to_contract_json; json.dump(to_contract_json(_mixed()), open('tests/fixtures/close_brief.json','w'), indent=2); print('ok')"` (match the existing file's `json.dump` style: check `git diff --stat` shows a sane diff, not a whole-file reformat; if the existing file is `indent=2` with a trailing newline, add `f.write('\n')`). Copy to `close_brief_v11.json`. Rewrite `test_frozen_fixture_carries_a_tiered_premium_section` as `test_frozen_fixture_carries_an_ungated_premium_section`: all four rows `full`, `calls` on each, equals `close_brief_v11.json`. Add `close_brief_v11.json` to `test_contract_schema.py`'s list.

- [ ] **Step 6: Run the gate** — `DATABASE_URL=… uv run pytest -q && uv run ruff check . && uv run mypy`. Expected: all pass. Then `grep -niE "\b(sell|write|buy|hold|roll)" tests/fixtures/close_brief.json` → no matches.

- [ ] **Step 7: Commit** — `git add apps/worker && git commit -m "feat(premium): §6 lists every held name, regime orders, calls[] priced in cents"`.

---

### Task 4: Narration prompt

**Files:**
- Modify: `apps/worker/worker/narrate.py:100-105` (`premium_ask`)
- Test: `apps/worker/tests/test_narrate.py` (~542-580)

- [ ] **Step 1: Write the failing test:**

```python
def test_premium_ask_says_every_held_name_is_listed() -> None:
    prompt = build_prompt(_with_full_premium_row())  # reuse the helper the existing premium tests build from
    assert "every held name is listed" in prompt
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_narrate.py -k every_held -v`. Expected: FAIL.

- [ ] **Step 3: Edit `premium_ask`** to: `'  "premium_note": the premium section\'s note. Every held name is listed; characterise which sit away from fair and why, in one or two sentences; never recommend an action, name a strike, or use the words sell, write, buy, or hold.\n'`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_narrate.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -am "feat(narrate): the §6 ask says every held name is listed"`.

---

### Task 5: Email renderer, preview sample, twenty-name size fixture

**Files:**
- Modify: `apps/web/emails/close-brief.tsx` (`Premium` ~488-520, `stripLine` ~527-537, `premiumHeadNote` ~476, the section wrapper ~128-141)
- Modify: `apps/web/emails/close-brief-preview.tsx` (premium rows ~96-180: `schema_version: 11`, add `iv30_chg`, `vix_close`, `iv_vix_book_median`, `calls` on three rows; keep one row with only the flat `call_*` fields and no `calls`, as the v10 fallback sample)
- Modify: `apps/web/emails/check-size.ts:14` (`FIXTURES` gains `"close_brief_v10.json"` and `"close_brief_premium20.json"`)
- Create: `apps/worker/tests/fixtures/close_brief_premium20.json` via a new frozen test in `apps/worker/tests/test_assemble.py`

**Interfaces:**
- Consumes: `Row.calls`, `CallQuote` from Task 2.
- Produces: `function Premium({ row }: { row: Row })`, `function callLine(c: CallQuote, currencySymbol: string): string`, `function premiumHeadNote(stale: string[], first: Row | undefined): string`.

- [ ] **Step 1: Freeze the twenty-name fixture** (worker side): add to `test_assemble.py`

```python
_FIXTURE_P20 = Path(__file__).parent / "fixtures" / "close_brief_premium20.json"

def _twenty() -> BriefObject:
    readings = {f"P{i:02d}": _reading(f"P{i:02d}", premium=("rich", "fair", "cheap")[i % 3],
                iv30_pctile=Decimal(5 + 4 * i)) for i in range(20)}
    return _assemble_premium(readings)

def test_matches_frozen_twenty_name_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OPTIONS_LIVE", False)
    assert to_contract_json(_twenty()) == json.loads(_FIXTURE_P20.read_text())
```

Generate the file the same way as Task 3 Step 5. Run `uv run pytest tests/test_assemble.py -k twenty -q` → PASS.

- [ ] **Step 2: Make the size check fail first.** Add the two fixture names to `FIXTURES`; run `pnpm --filter web email:check`. Expected: it renders (the renderer ignores `calls` today) and prints sizes; note the twenty-name size as the "before" number. If `close_brief_v10.json` fails to load because of a schema-version guard in `render.ts`, that is a bug to fix in this task.

- [ ] **Step 3: Rewrite `Premium`.** Head line: symbol, regime badge, `earnings {dayMonth}` badge, `thin` badge, then `· {n} contracts writable`. Own-vol line: `IV30 {iv30} ({signed iv30_chg})` — change omitted when null — then `· {ordinal}` or `· pctile pending` when `iv30` is non-null and the percentile is null, `· HV20 {hv20} · IV − HV {spread} · IV/VIX {ratio}× · term {signed slope}`. Calls: when `row.calls` is non-null, one `levelLine` per entry via `callLine`; otherwise the existing `stripLine(row)`. `callLine`: `{label} · {strike}C {dayMonth(expiry)} ({dte}d) · ${mid 2dp} = {dollars(contract_cents)}/contract[ · {dollars(writable_cents)} across {n}] · {premium_pct 1dp}% of spot[ ({signed premium_pct_chg} %)][ · $ {signed mid_chg 2dp}] · IV {iv 1dp}[ ({signed iv_chg 1dp})] · Δ {delta 2dp} · OI {open_interest}[ · strike {|moneyness − target| 1dp}% off target][ · same expiry as shorter tenor]`; labels `30d ATM`, `30d 10% OTM`, `7d ATM` from `(tenor, rule)`; a null `contract` prints `{label} · no two-sided quote`. Signed numbers through `signedOrDash` (U+2212). `premiumHeadNote(stale, first)`: `"against each name's own year and the tape"` + (first?.vix_close != null ? ` · VIX {2dp}` + (first.vix_pctile != null ? ` ({ordinal})` : ` (pctile pending)`) : "") + (first?.iv_vix_book_median != null ? ` · book median IV/VIX {2dp}×` : "") + the synthetic/stale suffixes. The wrapper passes `premium.rows.find(r => r.vix_close != null)`.

- [ ] **Step 4: Preview sample.** Bump `schema_version` to 11; give ASTS, SYM and the third full row `iv30_chg`, `vix_close: 18.4`, `iv_vix_book_median: 1.9` and a three-entry `calls` with realistic values (ASTS 30d ATM: contract `"ASTS  261120C00050000"`, strike 50, expiry `"2026-11-20"`, dte 27, mid 4.8, mid_chg 0.45, contract_cents 48000, writable_cents 192000, premium_pct 0.094, premium_pct_chg 0.021, iv 92.4, iv_chg 2.1, delta 0.52, open_interest 1204, both flags false). Leave RKLB on the flat fields only. Run `pnpm --filter web email` only if you want to eyeball it; it is not required.

- [ ] **Step 5: Verify** — `pnpm --filter web typecheck && pnpm check && pnpm --filter web email:check`. Expected: all four fixtures under 80KB. If `close_brief_premium20.json` is over, apply the spec's cut order (drop `OI`, then `Δ`, then time value — time value is not on the email line, so the third lever is nothing; report if still over). Then `grep -niE "\b(sell|selling|write|writing|buy|buying|hold|holding|roll)\b" apps/web/emails/close-brief.tsx apps/web/emails/close-brief-preview.tsx` → only pre-existing comment matches, none in rendered strings.

- [ ] **Step 6: Commit** — `git add apps/web/emails apps/worker/tests && git commit -m "feat(email): §6 ledger — every name, IV change, VIX in the head, calls in dollars"`.

---

### Task 6: Archive page

**Files:**
- Modify: `apps/web/app/briefs/[slug]/page.tsx` (premium section ~564-635, `stripLine` ~809-819, `S` styles ~851-930)

**Interfaces:**
- Consumes: `Row.calls`, `CallQuote`.
- Produces: `function CallRows({ calls, writable }: { calls: CallQuote[]; writable: number | null | undefined })` rendering one `<tr>` per entry under the name's row, and `callText(c: CallQuote): string` or per-cell `<td>`s (builder's choice).

- [ ] **Step 1: Add columns** `IV30 chg` (after `IV30`), `VIX` (after `VIX pctile`, prints `vix_close`), `Book median` (after `IV/VIX`). Replace the `Calls` cell: when `r.calls` is non-null, render after the name's `<tr>` one sub-row per entry spanning the table (`colSpan` = column count) carrying label, contract (`150C 2026-11-20`, dte), mid, `/contract` dollars, across-writable dollars, mid change, premium % and change, IV and change, delta, OI, and the two marks as text; when null, the old `stripLine(r)` cell as today. `premiumPending` footnote unchanged.

- [ ] **Step 2: Verify** — `pnpm --filter web typecheck && pnpm check`. Expected: clean. Render check: `pnpm dev` is optional; the page is a server component over Drizzle and needs a database, so the typecheck is the gate here.

- [ ] **Step 3: Commit** — `git commit -am "feat(archive): §6 table carries IV change, VIX, and a sub-row per selected call"`.

---

### Task 7: Docs — 04, 05, 07 (D39), 08 (M26)

**Files:**
- Modify: `docs/04-brief-object.md` (version table after row 10), `docs/05-content-spec.md:29` (§6 row), `docs/07-decisions.md` (append D39 after D38), `docs/08-milestones.md` (append M26 after M25), `docs/03-data-model.md` (one sentence under "### Premium (M22)": `iv30_chg` is derived at compute time from the prior session's stored `iv30` and not stored)

- [ ] **Step 1: docs/04** — add `| 11 | M26 | §6 ungated: every held name with a reading, `tier: full` up to 20; row fields `iv30_chg`, `vix_close`, `iv_vix_book_median`, `calls[]` (`call_quote`: contract, strike, expiry, dte, mid, mid_chg, contract_cents, writable_cents, premium_pct(_chg), time_value_pct, iv(_chg), delta, open_interest, target_miss, tenor_collapsed); the v10 flat `call_*` fields stay in the schema and are no longer emitted |`.

- [ ] **Step 2: docs/05** — rewrite the §6 row from the spec's Goals and Assembly sections: every held name; ordered rich/cheap first by distance from the median percentile, then fair; four lines per name; guards as badges; the VIX line in the section head; calls priced in dollars per contract and across writable contracts; note names held names with no reading and overflow past twenty; characterises, never recommends (D37, D39).

- [ ] **Step 3: docs/07** — write D39 in the house style (bold title, the context paragraph, italic *conditions*, *Rules out:*, *Reverses if:*). Content from the spec's Docs section: why the gate produced an empty section for a year; the M20 "never bare in email" rule amended for IV30 only, on the condition that a missing percentile prints as "pending" beside the value with IV − HV and IV/VIX on the same line; regime orders instead of gating; dollars per contract and across writable contracts are measurements of what is held; *Reverses if:* an action verb appears, as D37.

- [ ] **Step 4: docs/08** — `- [x] **M26 — §6 every held name, calls in dollars.** v11, `iv30_chg`/`book_median_ratio`/`mid_chg` on the reading, `calls[]`, `PREMIUM_CAP` 20, email ledger with the VIX line in the head, archive sub-rows, D39, docs 03/04/05/07/08. *Done when:* [copy the spec's Done-when bullets, abbreviated]. See `docs/superpowers/specs/2026-10-25-m26-premium-every-name-design.md`.`

- [ ] **Step 5: Verify** — `grep -niE "\b(sell|write|buy|hold)\b" docs/07-decisions.md | grep -i D39` returns only lines that name the words to forbid them. Commit: `git commit -am "docs(premium): v11 in 04, §6 rewrite in 05, D39, M26"`.

---

### Task 8: Whole-gate and end-to-end DB check

**Files:**
- Test: `apps/worker/tests/test_assemble_db.py` (new test) or `apps/worker/tests/test_volatility_db.py`

- [ ] **Step 1: Write an end-to-end DB test** that seeds and pulls synthetic chains for two held names the way `test_volatility_db._seed/_pull/_own` do (import those helpers or copy the minimal subset), runs `compute_and_store_volatility` for both sessions, passes the second session's readings to `assemble(..., kind="close", premium=readings)`, and asserts: both names present as `full`; each row's `calls` has three entries; the 30d ATM entry has a non-null `contract_cents` and, for the name with 250 shares, `writable_cents == contract_cents * 2`; `iv30_chg` non-null on the fresh name and null on the stale one.

- [ ] **Step 2: Run everything** —
  ```bash
  cd apps/worker && DATABASE_URL=postgresql+psycopg://localhost/session_brief_premium_v2 uv run pytest -q && uv run ruff check . && uv run mypy
  cd ../.. && pnpm contracts:gen && git diff --exit-code apps/worker/contracts apps/web/lib/contracts && pnpm --filter web typecheck && pnpm check && pnpm --filter web email:check
  cd apps/worker && DATABASE_URL=… uv run alembic heads
  ```
  Expected: pytest all pass (count ≥ 856 + new tests), no diff after codegen, every fixture under 80KB, one head `0020_options`.

- [ ] **Step 3: Commit** — `git commit -am "test(premium): end-to-end §6 from seeded chains to the assembled brief"`.
