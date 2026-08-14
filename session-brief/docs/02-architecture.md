# 02 — Architecture

*Vendor pricing verified August 2026. Re-check before committing.*

## Topology

```
                    ┌──────────────────────────────┐
   browser ────────▶│  apps/web  — Next.js 15      │
                    │  Vercel                      │
                    │  • book setup UI             │
                    │  • brief archive (renders    │
                    │    briefs.body)              │
                    │  • React Email templates     │
                    │  • auth, billing             │
                    └───────┬──────────────▲───────┘
                            │              │
                     reads/writes    POST /api/render/:brief_id
                            │        (shared secret — the only
                            ▼         service-to-service call)
                    ┌──────────────────────────────┐
                    │  Postgres (Supabase)         │
                    │  the shared surface          │
                    └───────▲──────────────────────┘
                            │
                    ┌───────┴──────────────────────┐
                    │  apps/worker — Python 3.12   │
                    │  Fly.io, long-running        │
                    │  • APScheduler               │
                    │  • ingest → compute →        │
                    │    assemble → narrate → send │
                    │  • owns Alembic migrations   │
                    └──────────────────────────────┘
                            │
              Tiingo · SEC EDGAR · Claude API · Resend
```

## Why two services

Next.js is the right tool for the UI, auth, billing, and — importantly — the email templates, because React Email is the best HTML-email authoring experience available and it lives naturally in the web app. Python is the right tool for the pipeline, because the compute is pandas-shaped: rolling correlations, z-scores, window functions over price series. Neither language is pleasant doing the other's job.

The cost is a second deploy target and a service boundary. The four rules below are what keep that boundary from rotting.

## The four boundary rules

**1. Postgres is the shared surface.** Not an internal REST API, not a message bus. Both services read and write the same tables. At this scale a shared database is a feature, not an anti-pattern.

**2. Python owns the schema, exclusively.** Alembic is the single migration path. The web app introspects the database to generate its query types (`drizzle-kit pull` or `kysely-codegen`) and never issues DDL. Two ORMs both believing they own a schema is the most reliable way to lose a weekend.

**3. Next.js writes intent; the worker does work.** "Send me one now" inserts a row into `jobs`; the worker picks it up with `SELECT … FOR UPDATE SKIP LOCKED`. No queue infrastructure, no webhook from web to worker, and the button stays responsive when the pipeline is slow.

**4. One exception, deliberately made.** The worker calls `POST /api/render/:brief_id` on the web app with a shared secret and receives `{ html, text }` back. This exists so the email template has exactly one source — React Email in the web app — rather than a Python MJML copy that drifts. The endpoint is pure: brief in, string out, no side effects. If the web app is down, the worker falls back to a plaintext-only send rather than skipping the brief.

## The shared contract

The BriefObject (`docs/04-brief-object.md`) is defined once as JSON Schema in `packages/contracts/`, and both sides are generated from it:

```
packages/contracts/brief-object.schema.json
        ├── datamodel-code-generator  → apps/worker/contracts/brief.py   (Pydantic)
        └── json-schema-to-typescript → apps/web/lib/contracts/brief.ts  (TS types)
```

`pnpm contracts:gen` regenerates both. CI fails if the generated files are dirty. This is the single most valuable piece of scaffolding in a two-language repo — without it, the renderer and the assembler drift within a month and the failure is silent.

## Pipeline

```
① ingest   raw vendor payloads → raw_payloads (jsonb), verbatim, idempotent
② normalize → bars_daily, quotes, fundamentals, events, news_items   (pure, no network)
③ compute  → metrics (long format)                                   (pure, testable)
④ assemble → BriefObject: suppression tiers, flags, claims           (pure)
⑤ narrate  → Claude API writes prose only, keyed by section id       (optional, non-fatal)
⑥ render   → web app /api/render → { html, text }
⑦ deliver  → deliveries row → Resend → status
```

Stages ② through ④ are pure functions over stored data. Freeze one session's `raw_payloads` as a fixture and the whole content pipeline is snapshot-testable without touching a vendor.

**The narration step must be non-fatal.** If the Claude call fails or returns malformed JSON, render with tables only and a one-line note. A brief with no prose is still useful; a brief that didn't send is not.

## Multi-user, from day one

Since this may become a product, two things are cheap now and expensive later:

- **`user_id` on every table**, including during single-user development. Backfilling it across a live schema is miserable.
- **Row-level security.** We're on Supabase, so tenancy is enforced with RLS policies keyed on `user_id`, never ad hoc in route handlers.

The scaling shape is favourable: **ingest cost is per unique symbol, not per user.** Fifty users sharing three hundred distinct tickers is one ingest pass. Fan out only at stage ④:

```
for symbol in union(all_users.symbols):  ingest, normalize, compute   # once
for user in active_users:                assemble, narrate, render, send
```

## The real product blocker: data licensing

This matters more than any technical decision here. Tiingo's free tier is licensed for **personal, internal use only, with no redistribution** — fine for your own book, not for paying users. Redistributing market data to third parties requires a commercial vendor plan and, depending on the data, exchange agreements.

**Design around it: build on end-of-day data.** The close brief is EOD-native and needs nothing else. The open brief works on prior-close data plus delayed pre-market quotes. Delayed and EOD data carry far lighter licensing than real-time, and the brief is a considered read at 08:15, not a trading screen. Sort the licensing before you take money, not after.

## Stack

| Layer | Choice | Note |
|---|---|---|
| Web | Next.js 15 App Router, TypeScript | Vercel |
| Email templates | React Email | Compiles to table-based HTML; see `docs/06-email.md` |
| Worker | Python 3.12, `uv`, APScheduler | Fly.io — long-running, no cold starts, no timeout ceiling |
| DB | Postgres 16 (Supabase) | `jsonb` for payloads and brief bodies; RLS for tenancy |
| Migrations | Alembic | Worker only |
| Web queries | Drizzle (introspected) or Kysely | Read-only against the worker's schema |
| Auth | Supabase Auth (or Clerk / Auth.js) | Only when user #2 exists |
| Billing | Stripe | Same |
| LLM | Claude API | Prose only |
| Monitoring | Healthchecks.io | Dead-man's switch per scheduled run |

## Scheduling

- Trading calendar from `exchange_calendars`. Half-days close at 13:00 and the close job must move with them.
- UTC at rest, `America/New_York` in logic. DST will otherwise shift the send by an hour twice a year.
- Stage the jobs: ingest 08:00, assemble 08:10, send 08:15. A slow ingest degrades rather than misses.
- Scheduler lives in the worker, not Vercel Cron — a rate-limited ingest across dozens of symbols will exceed serverless timeouts.
- Ping Healthchecks.io at the end of every run. Without it you find out about Thursday's failure on Monday.

## Vendors

| Need | Source | Cost |
|---|---|---|
| Daily OHLCV (EOD), adjusted close | Tiingo | Free, personal/non-commercial. `close` + `adjClose` map to `bars_daily.c` / `adj_c` |
| Earnings calendar, ex-div, macro releases | FinancialData.net — wired behind `FDN_API_KEY` (M16); synthetic fallback when unset | `earnings-calendar` / `dividends-calendar` / `economic-calendar`, spoken directly over httpx (`FdnClient`, D29 — not the `fdnpy` SDK). **Premium, $69/mo, personal use only**; redistribution needs Enterprise ($299/mo). With no key, §4 still seeds synthetically (M14). Lockup expiries are not covered by any tier, live or synthetic |
| Company news | FinancialData.net — wired behind `FDN_API_KEY` (M16); synthetic fallback when unset | `latest-news`, same Premium licensing gate. Turns on §3's `has_news` gate and feeds narration headlines when the key is set; narration ships without it otherwise (M8) |
| Cash, burn, shares outstanding | SEC EDGAR `companyfacts` | Free, authoritative XBRL, no key. Set a real User-Agent |
| Benchmark ETFs | Tiingo | Free, same EOD path as equities |
| Minute bars (deferred) | Massive (ex-Polygon.io) Starter | ~$29/mo — only for gap-fill and VWAP |
| Short interest | — | Skip v1; FINRA's bi-monthly cadence doesn't justify the plumbing |

Finnhub's free tier moved historical daily candles behind premium (403 on free keys), so it can't supply bars — Tiingo replaces it there. Polygon.io rebranded to Massive in late 2025 — same API and keys, new billing. Alpha Vantage's free tier is now 25 requests/day, which is unusable here.

The two former source gaps (earnings calendar, company news) now have a named provider, so they are a **pricing** question rather than an open search: FinancialData.net covers calendars, economic releases and news, all at Premium. It is wired behind `FDN_API_KEY` (M16) — unset, §4 still runs on the M14 synthetic seeder behind the provider seam, which is the same shape M15 uses for pre-market data. Finnhub's free tier remains a candidate if the licensing terms suit better.

Put every vendor behind a `MarketDataProvider` protocol (`daily_bars`, `quote`, `earnings_calendar`, `news`) from the first commit. You will switch, probably twice.

## Cost

| | Solo | ~100 users |
|---|---|---|
| Vercel | $0 | $20 |
| Fly.io worker | ~$5 | ~$15 |
| Supabase | $0 (free tier) | ~$25 (Pro) |
| Market data | $0 (non-commercial) | **commercial plan required** |
| Resend | $0 | ~$20 (4,200 sends/mo) |
| Claude API | ~$2–4 | ~$40 |
| **Total** | **~$10** | **$120 + data licensing** |

The data line is the one that decides whether this is a product.
