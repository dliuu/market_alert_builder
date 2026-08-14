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

## Catalysts — FinancialData.net (M17/M19)

Design: `docs/superpowers/specs/2026-08-14-m17-catalysts-design.md`.

### Q7 — Does `index-constituents` cover the Russell family at all?

**Blocks:** M19 index snapshot + diff · **Status:** open · **Ask first**

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

**Blocks:** M19 index snapshot + diff · **Status:** open · **High priority**

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

**Blocks:** M19 lockups, M17 `large_144` · **Status:** open, **downgraded by M18**

`pct_of_float` is what makes a supply signal legible: 1.2M shares means nothing,
0.24% of float means something.

*If unresolved:* `pct_of_float` is NULL and renders as **"size unknown"** rather
than the row being dropped. An unknown size is information; a missing row is not.
`large_144` (≥0.5% of float) cannot fire and degrades to `standard_144`.

*M18 changes the stakes here.* EDGAR supplies **shares outstanding**, which is an
upper bound on float — every restricted or insider-held share is counted in it.
Using it as the denominator makes `pct_of_float` systematically *understate* the
true percentage, so `large_144` fires less often than it should but never on a
name that doesn't deserve it. That is the right direction to be wrong in, and it
means the rule works on real data before this question is answered. A true float
figure sharpens it; it is no longer a precondition.

### Q5 — What is the actual `etf-holdings` call volume for the tracked set?

**Blocks:** M19 ETF snapshot · **Status:** open

`etf-holdings` pages at 50 records. SPY alone is ~10 calls; 30 ETFs is on the
order of 300 calls per full pass. The weekday stagger exists to spread this, but
the stagger width should be set from a measured number.

*If unresolved:* the rate-limit budget is a guess. Measure on the first live
pass, log call counts per endpoint per run, and set the stagger from the result.

### Q6 — Does `initial-public-offerings` cover de-SPACs and direct listings?

**Blocks:** M19 lockups · **Status:** open

The lockup calendar derives entirely from `listing_date`, so a listing type
missing from this endpoint produces no lockup signal at all — silently.

*If unresolved:* silent gaps. De-SPACs are also the population where the
180-day convention is *least* reliable, so they are doubly exposed: absent from
the calendar, and mis-dated when present. The per-symbol override table is the
manual escape hatch.

---

## Answered

*(none yet — move entries here with the answer and the date)*
