# Shanghai A-share briefs — design (CN-M1…CN-M3)

*2026-08-15. The Chinese-side counterpart to the US open/close pair: a second
brief pair over a real A-share book, sent on Shanghai wall-clock times. This
spec is the source of truth for the CN feature; the structural decisions are
recorded as D31 in `session-brief/docs/07-decisions.md`.*

## Product shape

Two more emails per **XSHG** trading day, for the same user, over a **separate
Chinese A-share book**:

- **CN open brief, 09:10 Asia/Shanghai** (20 min before the 09:30 bell) —
  forward-looking, no P&L. Overnight read = the US session that closed hours
  earlier (SPY et al., already in `bars_daily`), calendar, sector setup vs
  CSI 300. Always sends.
- **CN close brief, 15:20 Asia/Shanghai** (20 min after the 15:00 close) —
  backward-looking: full P&L + contribution attribution in CNY, suppression
  tiers, tape quality. Skips quiet sessions, like the US close brief.

Settled requirements: real positions with CNY cost basis; **CNY-native**
(integer fen at rest — the `*_cents` fields reread as "integer minor units" —
¥ display, no FX anywhere, the US and CN books never blend); benchmark
**CSI 300** (`000300.SS`; ETF `510300.SS` fallback); **both SSE and SZSE**
listings under the one XSHG calendar (identical hours/holidays); all display
in China time; the session date is the CST calendar date.

**v1 is skeleton-first** (the M14 pattern): no claims, flags, narration,
catalysts, or return-attribution for CN. Each of those needs either data with
no wired CN source or a deliberate design pass (e.g. a per-market claims
ledger), and lands as its own later milestone.

## Repository separation

Chinese-side docs live in `cn/docs/` (this folder). Chinese-side code lives in
dedicated packages **inside** the deployable apps — `apps/worker/worker_cn/`
and `apps/web/emails/cn/` — because Fly and Vercel build from `apps/worker` /
`apps/web` as their contexts; code outside them cannot ship. Build wiring is
two lines: `COPY worker_cn ./worker_cn` in the worker Dockerfile, and
`"worker_cn"` in the mypy `files` list (`package = false`, so nothing else).

Shared by necessity: the Alembic chain, the BriefObject schema, the calendar
generalization, and one import seam in the shared scheduler/CLI. The rule:
**CN logic only in `worker_cn/` and `emails/cn/`; shared files gain only
parameters and seams.**

## Design

**Book.** `sectors.market text NOT NULL DEFAULT 'US' CHECK (market IN
('US','CN'))`; holdings/lots inherit via `sector_id`. Reads gain a `:market`
filter (`compute._READ_LOTS` via a sectors join; `scheduler.book_symbols`;
`assemble_open` reads). `holdings UNIQUE(user_id, symbol)` stands because CN
symbols are suffixed and cannot collide.

**Symbols.** Internal CN symbols carry the exchange: `600519.SS`,
`300750.SZ`, benchmark `000300.SS`. Vendor ticker formats live inside
providers only (`TiingoProvider._vendor_symbol()`), never in internal symbols
or the database.

**Calendar.** `worker/calendar.py` grows a `MarketCalendar` wrapper
(exchange, tz, standard close + the existing session methods and
`local_date(now_utc)`); the existing module functions delegate to a `US`
instance so no call site changes. `worker_cn/calendar.py` holds
`CN = MarketCalendar("XSHG", Asia/Shanghai, 15:00)`. Verified live on
`exchange_calendars` 4.13.2: XSHG closes 07:00 UTC, National Day / Golden
Week / Chinese New Year correctly non-sessions. Invariant 8 becomes "UTC at
rest, the **exchange's** tz in logic."

**Kinds.** Two new `kind` values, `open_cn` / `close_cn`, so the briefs
unique key, slug/archive machinery, and `deliver_brief` generalize unchanged.
Contract `kind` enum widens and the object gains an **optional** top-level
`currency: "USD" | "CNY"` (absent ⇒ USD, so every stored v6 body keeps
validating); `schema_version` → 7. `vs_spy_bps` keeps its name — it
mechanically means "vs the book's benchmark" — with an amended description;
CN templates label it "vs CSI 300". `emails/render.ts` becomes an explicit
four-way dispatch that **throws** on an unknown kind (today it silently falls
back to the close template).

**Scheduler.** `next_kind_fire` is rebuilt as the **min of four independent
per-market fires**, each walking its own `cal.local_date(now_utc)` — a shared
"today" is wrong across markets (Shanghai's Monday 09:10 is Sunday evening
ET). CN open is wall-clock 09:10 CST on XSHG sessions; CN close is
`CN.close_or_standard(d) + 20min`, firing daily so it carries its own
dead-man heartbeat on `HEALTHCHECKS_CN_CLOSE_URL` (open gets
`HEALTHCHECKS_CN_OPEN_URL`). The CN close job mirrors `run_session_job`:
holiday ping → CN book symbols → poll for today's bars → assemble → deliver;
a missing bar fails loudly and **never sends stale**.

**Data.** v1 runs on `SyntheticCnBarsProvider` (`worker_cn/providers.py`),
implementing the existing `MarketDataProvider` protocol — only `daily_bars`,
emitting Tiingo-shaped records so `normalize.bars_from_payloads` is untouched;
prices are sha256-deterministic per `(symbol, date, salt)` and
window-independent; bars only on XSHG sessions. Ingest stamps
`source="synthetic-cn"` so `raw_payloads` never collides with live Tiingo
rows. The live switch is `CN_BARS_LIVE` (read only via
`worker_cn.config.cn_bars_are_synthetic()`), flipped only after the probe
passes — key presence can't derive this switch because `TIINGO_API_KEY`
already serves the US book, a recorded tension with D29. While synthetic,
every CN brief stamps `"cn_bars.synthetic"` into `data_quality.stale` and the
renderers show the banner (the M15 disclosure pattern).

**Assembly.** `worker_cn/assemble.py`, deliberately **not** the shared
`assemble_and_store` wrapper: that wrapper resolves due claims user-wide, and
a CN call at 15:20 CST would consume the US book's due claims before the US
close brief could report them. `assemble_cn_close_and_store` composes the
existing pure pieces — `compute_and_store(market="CN",
benchmark=CN_BENCHMARK)` → closes → tape → the pure shared `assemble()` with
`kind="close_cn"`, `currency="CNY"`, empty claims/flags/catalysts — so
invariant 3 (Σ contribution_bps == day_bps, exact) comes along untouched.
`assemble_cn_open_and_store` reuses `assemble_open`'s building blocks with
existing section ids only, so the section enum doesn't change. One shared
fix: `assemble_open._READ_EVENTS` gains a book-symbol filter
(`symbol IS NULL OR symbol = ANY(:book_symbols)`) so the two markets'
calendars never bleed into each other's briefs.

## External APIs (all of them, flagged)

| API | Role | Status |
|---|---|---|
| Tiingo daily prices | A-share EOD bars — the only live candidate wired | **Unverified for A-shares**: coverage, ticker format, `adjClose`, EOD publish latency after 15:00 CST, free-tier limits. Blocks CN-M3 only; probed by `tiingo-cn-probe`. See `open-questions.md`. |
| `exchange_calendars` XSHG | sessions/holidays/close | Verified live (local library, not a network API). |
| FinancialData.net | — | **Not used for CN** (US-listing-focused). Consequence: CN earnings/news/macro have **no wired source**; the calendar section runs on seeded `events`. |
| SEC EDGAR | — | Not applicable to SSE/SZSE issuers; CN fundamentals flags impossible in v1. |
| Claude API | narration | Not used in CN v1 (skeleton scope). |
| Resend | delivery | Existing, unchanged — two more sends per day. |
| Healthchecks.io | dead-man's switch | Two new checks (CN open, CN close). |
| FX rates | — | **Deliberately none.** CNY-native by design. |
| Stooq / AkShare / EODHD / Tushare Pro | fallback A-share EOD (and future calendars/news) | **Flagged, not built.** Stooq: keyless CSV, `.ss`/`.sz`. AkShare: free, scrapes Eastmoney/Sina — fragile, licensing murk. EODHD: ~$20/mo, confirmed `.SHG`/`.SHE` coverage. Tushare Pro: calendars/news behind registration + points. |

## Testing

The repo's standing patterns, in `apps/worker/tests/cn/`: frozen-fixture
snapshots for both CN briefs (`cn_close_brief.json`, `cn_open_brief.json`)
validated against the canonical JSON Schema; pure fire-time asserts on real
2026 XSHG dates (Golden Week, Chinese New Year, and the ET/CST
date-disagreement case); determinism and no-collision tests for the synthetic
provider; a mixed-book compute test proving the market filter and the exact
contribution identity; v6 fixtures still validating under the v7 schema.

## Risks

1. **Tiingo A-share coverage is unverified** — the whole reason CN-M1/M2 run
   synthetic; the swap is a provider constructor + one config flip.
2. **EOD publish latency vs the 15:20 send** — if the vendor publishes hours
   after the close, the send time renegotiates (config, not code); the brief
   never sends stale either way.
3. **No CN calendar/news source** — the open brief's calendar is seeded-only
   until a vendor is chosen (future milestone).
4. **CSI 300 availability** — `510300.SS` ETF fallback is a one-constant edit.
