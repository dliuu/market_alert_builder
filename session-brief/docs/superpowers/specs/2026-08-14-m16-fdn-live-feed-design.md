# M16 — FdnProvider: the live FinancialData.net feed (design)

*Status: designed 2026-08-14. Implements the swap D24 promised: synthetic → live, behind the existing seams, with no BriefObject shape change.*

## What this replaces

This morning's open brief carried a "synthetic feed · not live prices" banner because
three of its sections and two auxiliary inputs are invented:

| Surface | Today | After M16 (key present) |
|---|---|---|
| §2 overnight tape | `SyntheticPremarketProvider` + `TAPE_SEED_LEVELS` hash-moves | fdn `futures-prices` / `index-quotes` / `stock-quotes` |
| §3 pre-market names | hash-derived gaps in ±3.5%, fabricated volume | fdn `latest-prices` minute bars, filtered to the pre-market window |
| §4 calendar | `events_seed.py` fake earnings/ex-div/lockups | fdn `earnings-calendar` / `dividends-calendar` / `economic-calendar` |
| §3 `has_news` gate | dead code (`False` always) | fdn `latest-news`, held names only |
| Narration headlines | absent | same `latest-news` fetch, passed to the open prompt |

The close brief is untouched — it was already fully real (Tiingo EOD).

## Verified vendor facts (2026-08-14)

Fetched from fdnpy source and financialdata.net/documentation:

- Base URL `https://financialdata.net/api/v1/<endpoint>`; auth is a **`key` query
  parameter** (the vendor has no header auth — an accepted exception to the
  Tiingo header rule; we never log request URLs).
- `latest-prices` (Premium): current-week **minute bars** per identifier —
  `trading_symbol, time ("YYYY-MM-DD HH:MM:SS"), open, high, low, close, volume`.
- `futures-prices` (Standard): **daily bars** per identifier (`ZN`-style symbols) —
  `trading_symbol, date, open, high, low, close, volume`. No live overnight print.
- `index-quotes` / `forex-quotes` / `stock-quotes` (Premium): real-time, batched
  (`identifiers` comma-separated) — `trading_symbol, time, price, change,
  percentage_change` (+ naming fields).
- `earnings-calendar` (Standard): `trading_symbol, registrant_name, fiscal_period,
  earnings_announcement_date, …` — takes a single `date`.
- `dividends-calendar` (Standard): `trading_symbol, ex_dividend_date, …` — per `date`.
- `economic-calendar` (Standard): `indicator_name, country_code, release_date,
  release_time, actual/forecast/previous_value` — per `date`.
- `latest-news` (Premium): `trading_symbols, publication_time, article_headline,
  article_text, source, url` — 10 records per call, `offset` pagination.
- Record limit 300/request. Everything we need is covered by **Premium ($69/mo,
  personal use)**.

**Unverifiable offline** (needs a key): exact futures/index identifiers for our tape
(`ES`, `NQ`, `CL`, `^TNX`, `^VIX`, `^DXY` are best guesses), whether minute-bar
`time` is UTC, whether a futures daily bar dated *today* exists at 08:00 ET, and
`stock-quotes`' field names. The `fdn-probe` CLI (Task 8) verifies all four in one
run the day the key lands; until then every uncertain path **omits rather than
invents** — the standing M15 rule.

## Decisions

1. **Direct httpx, no fdnpy dependency.** fdnpy is a thin requests wrapper that
   parses prices as float. We replicate its endpoints with `parse_float=Decimal`
   (money invariant), matching how `TiingoProvider` already works. One new class,
   `FdnClient`, owns transport; everything above it is testable with
   `httpx.MockTransport`.
2. **The mode switch is the key.** `FDN_API_KEY` empty → today's synthetic path,
   bit-for-bit (tests and the deployed worker keep working). Key set → live.
   `constants.PREMARKET_FEED_IS_SYNTHETIC` becomes
   `config.premarket_feed_is_synthetic()` (derived, not hand-flipped); the
   renderers' banner keys off `data_quality.stale` exactly as before and comes off
   automatically.
3. **`FdnPremarketProvider` mirrors the synthetic's constructor** —
   `(client, prior_closes, session_date)`. Held names' `prev_close` still comes
   from `bars_daily` (authoritative); tape rows derive `prev_close` from the
   vendor (`price − change` for quotes, prior daily bar for futures), so
   `TAPE_SEED_LEVELS` is simply never consulted in live mode.
4. **Symbol translation is one table.** `constants.FDN_TAPE_IDENTIFIERS` maps each
   internal tape symbol → `(fdn endpoint, fdn identifier)`. Foreign-proxy ETFs
   (EWT, EWJ…) route to `stock-quotes` even though `tape_universe` tags them
   "index" — the adapter routes by table, not by seam method. Unmapped → omitted.
5. **Degradation over death.** A failed fdn endpoint yields an empty feed (section
   renders its omitted-note, M14 behavior), never a crashed 08:15 job. Events and
   news fetches are equally non-fatal. "A brief that arrives thin beats a brief
   that didn't arrive."
6. **Invariant 5 holds.** `FdnClient` captures every raw response;
   the scheduler stores them verbatim in `raw_payloads` (`source='fdn'`).
7. **News is a gate and a prompt block, not a schema change.** Headlines flip
   `clears_threshold(has_news=…)` for §3 and are appended to
   `build_open_prompt`; the digit guard already polices the output.
   `emit_premarket_gap` stays gap-only — news presence is not a directional call.
   No `schema_version` bump anywhere in M16.
8. **Lockup expiries go honestly absent in live mode.** No vendor covers them
   (docs/02); the synthetic seed inventing them was a placeholder, not a feature.

## Shape

```
worker/providers/fdn.py     FdnClient (transport + capture)
                            FdnPremarketProvider (the 4 seam methods)
                            FdnProvider gains real calendar methods
worker/events_fdn.py        3 calendars → CalendarEvent → events upsert
worker/news_fdn.py          latest-news → {symbol: [headline, …]}
worker/scheduler.py         live/synthetic branch + raw-payload store + events/news wiring
worker/assemble_open.py     news → has_news gate; passes headlines to narration
worker/narrate.py           build_open_prompt(obj, headlines=…)
worker/config.py            FDN_API_KEY, premarket_feed_is_synthetic()
worker/constants.py         FDN_TAPE_IDENTIFIERS; PREMARKET_FEED_IS_SYNTHETIC removed
worker/cli.py               fdn-probe
```

Plan: `docs/superpowers/plans/2026-08-14-m16-fdn-live-feed.md`.

## Manual verification runbook

Everything below is copy-pasteable. Run worker commands from `apps/worker/`
(`uv run …`); the SQL block runs against `DATABASE_URL` with any client
(`psql "$DATABASE_URL"` or Supabase's SQL editor).

### Synthetic mode (no key — verifiable today, before the Premium key exists)

```bash
uv run pytest
```
Expect: full suite green, including the M16 files (`test_config_fdn.py`,
`test_fdn.py`, `test_fdn_premarket.py`, `test_scheduler_fdn.py`,
`test_events_fdn.py`, `test_news_fdn.py`, `test_cli_fdn_probe.py`) and the M16
additions to `test_assemble_open.py`, `test_narrate.py`, `test_scheduler_open.py`.
DB-integration tests skip cleanly (not fail) when `DATABASE_URL` is unset
(`tests/conftest.py`) — a local run with no `.env` still exercises everything
except the Postgres round-trips.

```bash
uv run ruff check .
```
Expect: `All checks passed!`

```bash
uv run mypy
```
Expect (per the M16 DoD): clean. **As of this writing it is not** — `uv run
mypy` reports two pre-existing `attr-defined` errors in
`tests/test_scheduler_open.py` (`Module "worker.scheduler" does not explicitly
export attribute "config"`), introduced in `fix(m16): widen fdn feed error
handling; harden scheduler wiring tests` (Task 7 territory, already merged).
This is a real gap the offline DoD line doesn't yet meet — flag it rather than
paper over it; Task 9 is docs-only and does not touch code, so the fix (an
explicit re-export or `from worker.scheduler import config as scheduler_config`
in the two test call sites) is follow-up work, not done here.

```bash
uv run -m worker.cli fdn-probe
```
Expect (per the design intent): a clear error naming `FDN_API_KEY`, not a
traceback. **As currently implemented, this is not what happens**: `FdnClient.__init__`
raises a bare `RuntimeError("FDN_API_KEY is not set (see repo-root .env)")`
with nothing catching it in `_fdn_probe_cmd`/`main`, so the command exits with
a full Python traceback — the last line (`RuntimeError: FDN_API_KEY is not
set (see repo-root .env)`) does name the variable, but it is not the "clean
error" the tool's own docstring promises. Worth a one-line `try/except
RuntimeError` → `raise SystemExit(str(exc))` in `_fdn_probe_cmd` as follow-up;
out of scope for this docs-only task.

```bash
uv run -m worker.cli seed-premarket --date 2026-08-13
uv run -m worker.cli brief --kind open --date 2026-08-13 --dry-run
```
(Pick any date with `bars_daily` rows — `--date` defaults to the latest bar on
both commands if omitted.) `seed-premarket` first, because the synthetic
marker only fires when §2 actually has rows for the session
(`assemble_open.py`: `if tape_section["rows"] and
config.premarket_feed_is_synthetic(): …` — an empty §2 has nothing to mark
stale). Verified live against this repo's dev DB: a bare `brief --kind open
--dry-run` with no prior `seed-premarket` for that date printed
`"data_quality": {"missing": [], "stale": []}` — no marker, because §2 was
empty. Expect, after seeding: a printed BriefObject with `data_quality.stale`
containing `"overnight_tape.synthetic"`, and no `briefs` row written
(`--dry-run` rolls back the transaction). The web/email renderers key their
"synthetic feed · not live prices" banner off that same `data_quality.stale`
entry.

```bash
uv run -m worker.cli schedule --once --dry-run --kind open
```
This exact three-flag combination is accepted by `worker/cli.py`'s argparse
(`--once`, `--dry-run`, and `--kind {open,close}` are all real flags on the
`schedule` subcommand) — but check `_schedule`'s body before trusting the
combination does what it looks like: **`--dry-run` short-circuits before
`--once`/`--kind` are read at all.** It prints the next eight scheduler fires
(interleaving both `open` and `close` kinds) and returns; `--once` and
`--kind open` are silently ignored whenever `--dry-run` is also passed.
Verified live:
```
schedule: now 2026-08-14T17:45:34+00:00  (close = session close +45min, open = 08:15 ET fixed)
  close  2026-08-14T20:45:00+00:00   Fri 2026-08-14 16:45 ET   (session)
  close  2026-08-15T20:45:00+00:00   Sat 2026-08-15 16:45 ET   (non-session)
  close  2026-08-16T20:45:00+00:00   Sun 2026-08-16 16:45 ET   (non-session)
  open   2026-08-17T12:15:00+00:00   Mon 2026-08-17 08:15 ET   (session)
  close  2026-08-17T20:45:00+00:00   Mon 2026-08-17 16:45 ET   (session)
  ...
```
Eight lines total, alternating `open`/`close`, nothing sent, nothing written —
the safe smoke check per D20. Dropping `--dry-run` is **not** safe in the same
way: `run_open_session_job` (like `run_session_job`) calls `deliver_brief`
internally, so `uv run -m worker.cli schedule --once --kind open` is a **live
run that will actually send** if `RESEND_API_KEY`/`BRIEF_FROM`/recipient are
configured — the same "`--once` sends" warning D20 already gives for the close
job, now true of the open one too. Use `--dry-run` for the smoke check; only
drop it when you intend to send.

### Live mode (once the Premium key is set)

```bash
fly secrets set FDN_API_KEY=...   # run from apps/worker/, per fly.toml's app name
uv run -m worker.cli fdn-probe
```
Expect: one line per check — `FDN_TAPE_IDENTIFIERS` entries (futures/index/
forex/stock-quotes identifier guesses), a held-name `latest-prices` UTC-
timestamp check, an ES `futures-prices` session-dated-bar check, a
`stock-quotes` field-name dump, the three calendar endpoints, and `latest-news`
(twice — once for reachability, once for the page-size assumption). Every
check prints `✓ …` or `✗ …`; the command always exits 0 (`_fdn_probe`'s own
docstring: "read-only … a human reads the ✓/✗ lines"). The key itself never
appears in any line (`_safe_error` strips it out of httpx exception text).

**On a ✗ against a tape identifier** (e.g. `^DXY` or `^TNX` turns out wrong):
edit the mapping in `worker/constants.py`'s `FDN_TAPE_IDENTIFIERS` — it's a
`dict[str, tuple[str, str]]` of internal symbol → `(fdn endpoint, fdn
identifier)` — then re-run `fdn-probe`. No other code changes; a symbol left
unmapped is simply omitted from §2, never invented (docs/07 D28).

**Confirming the switch actually flipped**, once the next open brief has sent:
- The email/web brief must **not** carry the `"synthetic feed · not live
  prices"` marker (`apps/web/emails/open-brief.tsx`,
  `apps/web/app/briefs/[slug]/page.tsx` both key it off
  `data_quality.stale.includes("overnight_tape.synthetic")`).
- Pull the stored object and check directly:
  ```bash
  uv run -m worker.cli brief --kind open --date <today> --dry-run
  ```
  Expect: `data_quality.stale` does **not** contain `"overnight_tape.synthetic"`,
  and §2/§3 rows carry real, non-round-number levels (not the invented
  `TAPE_SEED_LEVELS` nominal figures like `5620.00`/`103.00`/`15.00`).

**Confirming live rows actually landed**, against `DATABASE_URL`:
```sql
SELECT endpoint, symbol, as_of, fetched_at
FROM raw_payloads
WHERE source = 'fdn'
ORDER BY fetched_at DESC
LIMIT 20;
```
Expect: rows across the endpoints the morning job touches — `futures-prices`,
`index-quotes`, `stock-quotes`, `latest-prices`, `earnings-calendar`,
`dividends-calendar`, `economic-calendar`, `latest-news` — each `as_of` the
session date, `fetched_at` around the 08:00 ET ingest fire.

```sql
SELECT symbol, session_date, captured_at, last, prev_close, extended_last, extended_v
FROM quotes
WHERE session_date = '<today's session date>'
ORDER BY symbol;
```
Expect: rows for the tape symbols (`ES=F`, `NQ=F`, `CL=F`, `^TNX`, `^VIX`,
`DXY`, and the foreign-proxy ETFs) and for held names that moved pre-market,
with `extended_last`/`extended_v` populated for the held-name rows and `last`/
`prev_close` populated for the tape rows — no more `bars_daily`-shaped
recursion back to `TAPE_SEED_LEVELS`.
