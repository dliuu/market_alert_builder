# CN milestones

Sequenced, with testable definitions of done — the `docs/08-milestones.md`
discipline, numbered separately (CN-M1…) so the Chinese side evolves
independently. Design: `2026-08-15-shanghai-briefs-design.md`.

- [x] **CN-M1 — CN close brief, CLI-driven, synthetic bars.** The plumbing
  milestone: `sectors.market` (migration 0015), `worker_cn/` package scaffold +
  Dockerfile/mypy wiring, `MarketCalendar` generalization with the XSHG
  instance, `signed_money(minor_units, currency)`, `compute_and_store`
  market/benchmark params, `SyntheticCnBarsProvider` +
  `source="synthetic-cn"` ingest, contract v7 (`open_cn`/`close_cn` kinds +
  optional `currency`, migration 0016), CN email templates, and
  `assemble_cn_close_and_store`. *Done when:* a seeded CN book (SSE + SZSE
  names + `000300.SS`) backfills via `backfill --market cn`, and
  `brief --kind close_cn` produces a frozen-fixture-snapshotted CN close brief
  — `currency: "CNY"`, ¥ subject, Σ contribution_bps == day_bps exact, tiers
  and tape quality populated, `cn_bars.synthetic` disclosed — readable at
  `/briefs/<date>-close_cn`; a mixed US+CN book computes each market against
  its own benchmark with existing US fixtures byte-identical; v6 stored bodies
  still validate under the v7 schema; `pnpm contracts:gen` green.

- [ ] **CN-M2 — CN open brief + four-kind scheduler.**
  `assemble_cn_open_and_store` (overnight = US session closes, CN-filtered
  calendar, sector setup vs CSI 300, always sends), the `_READ_EVENTS`
  book-symbol filter on both markets, `next_kind_fire` as min-of-four
  independent per-market fires, `run_cn_open_session_job` /
  `run_cn_close_session_job` with their own Healthchecks URLs. *Done when:*
  `schedule --dry-run` prints all four fires on a shared session day (09:10
  CST = 01:10 UTC, 15:20 CST = 07:20 UTC); Golden Week skips both CN fires
  while US fires proceed and vice versa for a US holiday; the
  date-disagreement test passes (Sunday 23:00 ET → Monday's `cn_open` next,
  even when that Monday is a US holiday); a CN calendar section never names a
  US-book symbol and vice versa; five CN sessions run untouched on the live
  deploy, including one CN-only holiday correctly skipped (manual soak, box
  stays unticked until observed). *Status:* code complete; only the five-
  session live soak is pending, so the box stays unticked until it's observed.

- [x] **CN-M3 — Live Tiingo A-share swap behind the probe.** `tiingo-cn-probe`
  answers CN-Q1…CN-Q5 in `open-questions.md`; `TiingoProvider._vendor_symbol()`
  maps `.SS`/`.SZ` to the verified format; `CN_BARS_LIVE=true` switches the
  feed. *Done when:* the probe's answers are recorded inline; with the flag on,
  the next CN close brief sends with real bars and **without** the
  `cn_bars.synthetic` marker; with it off, the same code reproduces the
  synthetic brief bit-for-bit; `uv run pytest` passes in both modes.
  *Contingencies:* EOD publish latency ⇒ renegotiate the send time (config
  only); no Tiingo coverage ⇒ synthetic stays, escalate to
  Stooq/AkShare/EODHD per the fallback table. *Status:* DoD met except CN-Q4's
  same-day-timing half — the bar-existence check ran off-session (Sunday), so
  same-day availability at ~15:20 CST on an actual session day is still
  unverified (`open-questions.md`); `CN_SEND_DELAY_MINUTES` and the close
  job's fail-loud bar poll are the safety net until it's run at the right
  wall-clock time.

**Deferred (each its own future milestone, in rough order of value):** CN
claims + accountability loop (needs a per-market claims design), CN narration
(feed it CN headlines — needs a news source), CN flags (needs fundamentals),
CN calendar/news vendor (Tushare/AkShare evaluation), CN return attribution
(needs 120 sessions of real bars + theme baskets).

## Switch-on procedure: `CN_BARS_LIVE` requires a synthetic-history purge first

**Do this before the first live CN close, same requirement M16 set for the
premarket feed (commit `a6d1f6e`, docs/08).** `bars_daily`'s RVOL (30-session
window, `worker/tape.py`'s `RVOL_WINDOW`) averages the *prior* sessions'
volume. Every CN row written under the synthetic provider is hash-fabricated,
so on switch-on a real today's volume gets divided by a partly-invented
30-session base for roughly six weeks — with the `cn_bars.synthetic`
disclosure banner now off, since the feed is live. There is no provenance
column and no code that guesses which rows are synthetic; this is a one-time
human step, not something the pipeline can self-clean.

```sql
-- Every CN bar written before the key went live is synthetic. Scope to the
-- book's own symbols only (never touch the US side's bars_daily rows).
DELETE FROM bars_daily
WHERE symbol IN (SELECT DISTINCT h.symbol FROM holdings h
                  JOIN sectors s ON s.id = h.sector_id AND s.user_id = h.user_id
                  WHERE s.market = 'CN')
   OR symbol = '510300.SS';  -- worker_cn.constants.CN_BENCHMARK
```

The synthetic-cn `raw_payloads` rows are **not** purged — they stay as inert
history, same as any other stage-① capture; only the derived `bars_daily`
rows need to go. Immediately after the purge, rerun the backfill live so
tape/RVOL denominators are real before the first live send:

```bash
CN_BARS_LIVE=true uv run -m worker.cli backfill --market cn --days 90
```

Skipping this step means the first live CN close brief's RVOL column blends
invented and real volumes for ~6 weeks with no disclosure that anything is
off — the exact failure the disclosure banner exists to prevent.
