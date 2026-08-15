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

**Chinese-side questions live in `cn/docs/open-questions.md`** (CN-Q1…CN-Q5:
Tiingo A-share coverage/format, `adjClose`, limits, EOD publish latency,
CSI 300 availability) — kept there so the two sides stay separable.

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

---

## Answered

*(Q1–Q6 settled 2026-08-15 by user decision — recorded as design rulings, not
vendor-probed facts. Where an entry's original fallback still guards a residual
risk, it is restated with the answer.)*

### Q1 — Does `index-constituents` reflect announcement or effective membership?

**Answered 2026-08-15:** treat it as reflecting **announcement** membership.
The index differ takes its detection date as the announcement date; no
supplementary `sec-press-releases` / `latest-news` check is built for v1.

*Residual guard:* the M18 DoD test (snapshot across a known reconstitution,
compare detection date to the published announcement date) stays — it is now a
verification of this answer rather than an open probe. If it shows the endpoint
is actually effective-only, the original fallback (a news-based announcement
supplement) reactivates and this answer reopens.

### Q2 — Does `insider-transactions` distinguish tax-withholding dispositions and option exercises at grant price?

**Answered 2026-08-15:** **no differentiation for now** — all Form 4
dispositions are counted alike by the M17 detectors, and the work will circle
back to this. Consequence accepted: elevated false positives on
`outsized_sale` and `cluster` (a CFO surrendering shares to cover withholding
on a vest will look like a CFO selling). The severity-reduction-and-annotate
mitigation from the original entry is **not** applied in v1; when the work
circles back, the original test still applies (pull a known vest-heavy issuer
and inspect `transaction_type` cardinality against the Form 4 footnote codes).

### Q3 — Does `proposed-sales` expose a stable filing identifier?

**Answered 2026-08-15:** **use the deterministic hash as the natural key** —
`symbol|insider_name|filing_date|shares_proposed` — rather than depending on
an accession number. Accepted cost, as documented: slightly weaker dedup — an
amended filing with identical fields collapses into the original. If the
endpoint does expose an accession number, it may be recorded in `detail`
jsonb for reference, but the hash stays the key.

### Q4 — Does `securities-information` provide public float reliably?

**Answered 2026-08-15:** **skip % of float for now** — `securities-information`
is not called; `pct_of_float` is NULL everywhere and renders as **"size
unknown"** (an unknown size is information; a missing row is not).
Consequence accepted: `large_144` (≥0.5% of float) cannot fire and every
proposed sale grades as `standard_144`. Revisit when float sourcing is
worth a probe.

### Q5 — What is the actual `etf-holdings` call volume for the tracked set?

**Answered 2026-08-15:** the budget is set rather than measured — **300 calls
per week** is the rate limit for the ETF pass. The token-bucket limiter and
the weekday stagger are sized to it (~43 calls/day across the tracked ETFs;
SPY alone is ~10 calls at 50 records/page, so the tracked-ETF list must fit
the weekly budget rather than the budget stretching to the list). Log call
counts per endpoint per run so the first live pass can confirm the list fits;
if it doesn't, shrink the tracked set — the budget is the fixed point.

### Q6 — Does `initial-public-offerings` cover de-SPACs and direct listings?

**Answered 2026-08-15:** **out of scope for now** — the lockup calendar does
not need to cover de-SPACs or direct listings yet. Consequence accepted:
silent gaps for those listing types (and the 180-day convention is least
reliable exactly there, so this is also the population where a wrong date
would have been likeliest). The per-symbol override table remains the manual
escape hatch if a specific name matters before this reopens.
