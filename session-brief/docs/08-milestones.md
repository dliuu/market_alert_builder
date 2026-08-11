# 08 — Milestones

Sequenced. Each has a definition of done that is testable, not vibes.

- [x] **M0 — Scaffold.** Monorepo, both apps boot, Supabase connected, Alembic runs, `pnpm contracts:gen` produces both generated files and CI fails when they're dirty.
- [x] **M1 — Book CRUD.** Sectors, holdings, lots in Next.js. *Done when:* you enter your real book and it survives a redeploy.
- [ ] **M2 — Ingest + normalize.** One vendor behind `MarketDataProvider`. *Done when:* `bars_daily` fills nightly for your symbols and a replay from `raw_payloads` reproduces it byte-for-byte.
- [ ] **M3 — Compute: returns, P&L, contribution.** *Done when:* numbers tie out against your broker to the cent, and `Σ contribution_bps == book day_bps` in a property test.
- [ ] **M4 — Assemble → BriefObject → web page.** *Done when:* you can read a close brief at `/briefs/<date>-close` and a snapshot test asserts the object from a frozen fixture.
- [ ] **M5 — Suppression + tape quality.** *Done when:* a quiet session produces a visibly shorter brief and the roll-up line names the skipped tickers.
- [ ] **M6 — Claims + resolution.** *Done when:* the close brief scores the same day's open brief and outcomes persist in `claims`.
- [ ] **M7 — Flags: position risk + correlation.** *Done when:* thresholds fire correctly on a synthetic fixture and the weekly rate limit holds across three consecutive briefs.
- [ ] **M8 — Narration.** *Done when:* prose appears, and revoking the Claude API key still produces a valid, sendable brief.
- [ ] **M9 — React Email + Resend + DNS.** *Done when:* it lands in your inbox looking like the design reference, in Gmail *and* Outlook, under 80KB, with a plaintext part.
- [ ] **M10 — Scheduler + dead-man's switch.** *Done when:* it runs five consecutive sessions untouched, including one market holiday it correctly skips.

**M3 is the one to be pedantic about.** If contribution bps don't sum to the book return, every downstream insight is quietly wrong and you won't notice for a month.

**M0–M8 need no email at all.** Resist wiring delivery early.
