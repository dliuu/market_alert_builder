# CN open questions

Vendor-behaviour questions the Chinese-side feature cannot answer from code.
Same rules as `session-brief/docs/open-questions.md`: each entry blocks a
specific piece of work, needs a live probe to settle, and has a written
fallback so nothing waits on it to start. An entry leaves here by being
answered — record the answer inline, then fold anything load-bearing into D31's
successors in the shared decision log.

---

## A-share EOD bars — Tiingo (CN-M3)

Design: `2026-08-15-shanghai-briefs-design.md`. All five are answered by one
`tiingo-cn-probe` run (plus one run at ~15:20 CST for Q4); until then CN-M1/M2
run on the synthetic seam and every brief carries the `cn_bars.synthetic`
disclosure.

### CN-Q1 — Does Tiingo's free tier serve SSE/SZSE tickers at all, and in what format?

**Blocks:** CN-M3 live swap · **Status:** open · **Ask first**

Tiingo advertises Chinese equity coverage, but the free-tier entitlement and
the ticker format (`600519-SS`? `600519-SHG`? bare `600519`?) are undocumented
guesses. `_vendor_symbol()` is written against a placeholder mapping until the
probe reports which format resolves.

*If unavailable:* the synthetic seam stays; escalate down the fallback table —
Stooq (keyless, `.ss`/`.sz`), AkShare (free, scraping fragility), EODHD
(~$20/mo, confirmed coverage). The provider seam makes this a constructor
change.

### CN-Q2 — Is `adjClose` real for A-shares?

**Blocks:** CN-M3 · **Status:** open

Every return in the pipeline reads adjusted closes. A vendor that mirrors
`close` into `adjClose` for A-shares silently corrupts every CN return across
ex-div dates.

*Test:* pull a name across a known dividend (A-shares pay annually, mostly
June–July) and check the two columns diverge on the ex-date.
*If unresolved:* returns are computed but flagged; do not trust cross-ex-date
windows.

### CN-Q3 — What are the free-tier limits with ~10 CN symbols added?

**Blocks:** CN-M3 · **Status:** open

The CN book adds held names + `000300.SS` to the nightly ingest. Tiingo's free
tier has hourly/daily request caps shared with the US book's ingest.

*If unresolved:* measure on the first live pass (the M18 CN-Q5 precedent) and
stagger CN ingest away from the US window if needed.

### CN-Q4 — How soon after 15:00 CST does the EOD bar publish?

**Blocks:** the 15:20 CST close-brief send time · **Status:** open · **High priority**

The CN close brief polls for today's bar before assembling (D20 semantics: a
missing bar fails loudly, never a stale send). If Tiingo publishes A-share EOD
hours after the close, a 15:20 send can never carry same-day bars.

*Test:* run the probe at ~15:20 CST on a session day and check whether today's
bar exists.
*If late:* renegotiate the send time (`CN_SEND_DELAY_MINUTES` — e.g. 17:30
CST, or a T+1-morning close brief). A config change, not code.

### CN-Q5 — Is CSI 300 servable as an index (`000300`), or only via the `510300` ETF?

**Blocks:** CN benchmark line · **Status:** open

The vs-benchmark line needs a CSI 300 daily series. Many vendors serve only
tradables.

*If index unavailable:* `CN_BENCHMARK = "510300.SS"` (one constant). Tracking
error vs the index is a documented caveat, not a blocker.

---

## No wired source (flagged, deferred — not probing yet)

- **CN earnings calendar / company news / macro releases:** FinancialData.net
  is US-focused and SEC EDGAR does not apply. The open brief's calendar section
  runs on seeded `events` until a CN calendar vendor is chosen (candidates:
  Tushare Pro, AkShare). This is why claims/flags/narration are out of the CN
  v1 scope.
- **CN fundamentals (cash/burn/shares out):** no EDGAR equivalent wired;
  position-risk flags are not buildable for CN in v1.

---

## Answered

*(none yet)*
