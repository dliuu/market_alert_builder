# Open questions

Questions about **vendor behaviour** that code cannot answer and guessing would
get wrong. Each one blocks a specific piece of work, needs a live API key to
settle, and has a written fallback so nothing waits on it to start.

This is deliberately not the decision log. `docs/07-decisions.md` records what we
chose and why; this file records what we don't yet know. An entry leaves here by
being answered — record the answer inline, then fold anything load-bearing into
the decision log.

**Rule:** where a spec says *VERIFY*, write the code against the documented
contract, guard it, and add the question here. Do not guess and do not silently
degrade.

---

## Pre-market feed — FinancialData.net (M16)

Design: `docs/superpowers/specs/2026-08-14-m16-fdn-live-feed-design.md` §"What
the probe is really asking, and what to do with each answer". All three are
**unresolved as of this writing** — nothing below has been observed against the
live vendor. Each fails silently (an empty section, not an error), so the code
ships the optimistic assumption in every case; `fdn-probe` prints a verdict line
for each once `FDN_API_KEY` is set.

### Q8 — Does `latest-prices` paginate oldest-first past the 300-record cap?

**Blocks:** §3 pre-market names going live · **Status:** open

`FdnPremarketProvider.get_latest_prices` fetches with no pagination. A current
week of minute bars is ~1,950 records against the vendor's documented 300-record
cap per request. If the response is oldest-first, it ends days before today, the
pre-market window filter matches nothing, and every held name is omitted — §3
renders its omitted-note and the email has no pre-market section at all.

*If unresolved (oldest-first truncation):* add pagination to
`get_latest_prices` — page with `offset` until a record's `time` reaches the
window, or narrow the request if the endpoint accepts a date/time bound. Do
**not** widen the window filter to compensate; that would admit yesterday's
prints as this morning's.

*Test:* `fdn-probe` check 1's verdict line — problem if the count is at/near 300
and the latest `time` is hours or days stale; fine if the count is well under
300, or ~300 with the latest `time` within minutes of `now(UTC)`.

### Q9 — Does `futures-prices` ever carry a session-dated bar at 08:00 ET?

**Blocks:** §2 overnight tape, 3 of its 6 fixed rows (ES=F/NQ=F/CL=F) ·
**Status:** open

`_futures_rows` requires `len(bars) >= 2` and `bars[0]["date"] == session_date`.
The endpoint returns **daily** bars with no live overnight print, so a bar dated
*today* very probably does not exist yet at 08:00 ET — the likely outcome is
`NO session-dated bar`, and the three futures rows silently fall out of §2.

*If unresolved:* a decision, not a patch, in preference order — (1) move
futures to a quote endpoint if one covers them (check whether `index-quotes`
accepts the futures identifiers), the only option giving a genuine overnight
read; (2) accept the omission, §2 renders three rows and the tape is thinner —
current behavior, no code change; (3) redefine the row as prior-settle-vs.-the-
settle-before using `bars[0]`/`bars[1]` whatever their dates, but *only if the
label changes with it* — presenting a stale daily change as an overnight move
is inventing a number.

*Test:* `fdn-probe` check on `futures-prices ES (session-dated bar)`.

### Q10 — Does `latest-news` honor a per-symbol filter, or is the news gate decorative?

**Blocks:** §3's `has_news` gate, narration headline block · **Status:** open

`fetch_held_news` pulls 3 pages × 10 market-wide articles and keeps whatever
mentions a held name. Thirty most-recent market-wide headlines will rarely
touch any of ~10 holdings, so `has_news` stays `False` and the narration
headline block stays empty — a feature that reports as working while doing
nothing.

*If HONOURED* (every returned record mentions the symbol): rework
`fetch_held_news` to fetch per held symbol instead of paging the market-wide
feed — one call per name, drop the `_PAGES` loop.

*If IGNORED* (the param was silently dropped): the guess was wrong, not the
idea — check the vendor docs for the real filter parameter name and re-probe
via check 7's parameter in `worker/cli.py`. If no per-symbol filter exists at
any tier, keep market-wide paging and say so rather than leaving a reader to
assume §3's news threshold is live.

*Test:* either way the failure is silent — verify by eye on the first live
morning. The job prints `open <date>: N calendar events, news for [...]`; an
always-empty list there is this finding, not a quiet news day.

---

## Catalysts — FinancialData.net (M17/M18)

Design: `docs/superpowers/specs/2026-08-14-m17-catalysts-design.md`.

### Q7 — Does `index-constituents` cover the Russell family at all?

**Blocks:** M18 index snapshot + diff · **Status:** open · **Ask first**

The design tracks six indices: `^GSPC`, S&P 400, S&P 600, Russell 1000, Russell
2000, Russell 3000. FTSE Russell licenses constituent data separately from S&P
Dow Jones, and a vendor carrying one does not imply the other.

*If unavailable:* three of the six tracked indices don't exist, and roughly half
the index work with them. Russell reconstitution (annual, late June) is also the
single highest-impact membership event for small and mid caps, so losing it
changes what the index source is worth.

*Why first:* it is one API call and it resizes the milestone. Answer it before
writing the differ, not after.

### Q1 — Does `index-constituents` reflect announcement or effective membership?

**Blocks:** M18 index snapshot + diff · **Status:** open · **High priority**

S&P announces changes after the close, typically Friday, effective roughly a week
later. If the endpoint reflects **effective** membership only, a snapshot diff
detects the change ~5 trading days *after* it was public.

*If unresolved:* the index source silently becomes a lagging indicator while
presenting as a catalyst. A supplementary check against `sec-press-releases` or
`latest-news` would be required to recover the announcement date.

*Test:* snapshot across a known reconstitution and compare the detection date to
the published announcement date.

### Q2 — Does `insider-transactions` distinguish tax-withholding dispositions and option exercises at grant price?

**Blocks:** M17 insider detectors · **Status:** open

Both are mechanical, non-discretionary, and carry no signal. A CFO surrendering
shares to cover withholding on a vest is not a CFO selling.

*If unresolved:* do **not** silently include them and do **not** silently drop
them. Reduce severity by 1 and annotate the item — an ambiguous signal shown as
ambiguous is honest; either silent choice is not. Expect elevated false
positives on `outsized_sale` and `cluster` until answered.

*Test:* pull a known vest-heavy issuer and inspect `transaction_type` cardinality
against the Form 4 footnote codes.

### Q3 — Does `proposed-sales` expose a stable filing identifier?

**Blocks:** M17 proposed-sales ingest · **Status:** open

`natural_key` prefers an accession number if one is exposed.

*If unresolved:* fall back to a deterministic hash of
`symbol|insider_name|filing_date|shares_proposed`. Slightly weaker dedup — an
amended filing with identical fields collapses into the original. Document
whichever is used.

### Q4 — Does `securities-information` provide public float reliably?

**Blocks:** M18 lockups, M17 `large_144` · **Status:** open

`pct_of_float` is what makes a supply signal legible: 1.2M shares means nothing,
0.24% of float means something.

*If unresolved:* `pct_of_float` is NULL and renders as **"size unknown"** rather
than the row being dropped. An unknown size is information; a missing row is not.
`large_144` (≥0.5% of float) cannot fire and degrades to `standard_144`.

### Q5 — What is the actual `etf-holdings` call volume for the tracked set?

**Blocks:** M18 ETF snapshot · **Status:** open

`etf-holdings` pages at 50 records. SPY alone is ~10 calls; 30 ETFs is on the
order of 300 calls per full pass. The weekday stagger exists to spread this, but
the stagger width should be set from a measured number.

*If unresolved:* the rate-limit budget is a guess. Measure on the first live
pass, log call counts per endpoint per run, and set the stagger from the result.

### Q6 — Does `initial-public-offerings` cover de-SPACs and direct listings?

**Blocks:** M18 lockups · **Status:** open

The lockup calendar derives entirely from `listing_date`, so a listing type
missing from this endpoint produces no lockup signal at all — silently.

*If unresolved:* silent gaps. De-SPACs are also the population where the
180-day convention is *least* reliable, so they are doubly exposed: absent from
the calendar, and mis-dated when present. The per-symbol override table is the
manual escape hatch.

---

## Answered

*(none yet — move entries here with the answer and the date)*
