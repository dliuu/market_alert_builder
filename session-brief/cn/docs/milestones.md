# CN milestones

Sequenced, with testable definitions of done — the `docs/08-milestones.md`
discipline, numbered separately (CN-M1…) so the Chinese side evolves
independently. Design: `2026-08-15-shanghai-briefs-design.md`.

- [ ] **CN-M1 — CN close brief, CLI-driven, synthetic bars.** The plumbing
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
  stays unticked until observed).

- [ ] **CN-M3 — Live Tiingo A-share swap behind the probe.** `tiingo-cn-probe`
  answers CN-Q1…CN-Q5 in `open-questions.md`; `TiingoProvider._vendor_symbol()`
  maps `.SS`/`.SZ` to the verified format; `CN_BARS_LIVE=true` switches the
  feed. *Done when:* the probe's answers are recorded inline; with the flag on,
  the next CN close brief sends with real bars and **without** the
  `cn_bars.synthetic` marker; with it off, the same code reproduces the
  synthetic brief bit-for-bit; `uv run pytest` passes in both modes.
  *Contingencies:* EOD publish latency ⇒ renegotiate the send time (config
  only); no Tiingo coverage ⇒ synthetic stays, escalate to
  Stooq/AkShare/EODHD per the fallback table.

**Deferred (each its own future milestone, in rough order of value):** CN
claims + accountability loop (needs a per-market claims design), CN narration
(feed it CN headlines — needs a news source), CN flags (needs fundamentals),
CN calendar/news vendor (Tushare/AkShare evaluation), CN return attribution
(needs 120 sessions of real bars + theme baskets).
