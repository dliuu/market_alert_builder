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
