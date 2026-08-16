# CN open questions

Vendor-behaviour questions the Chinese-side feature cannot answer from code.
Same rules as `session-brief/docs/open-questions.md`: each entry blocks a
specific piece of work, needs a live probe to settle, and has a written
fallback so nothing waits on it to start. An entry leaves here by being
answered — record the answer inline, then fold anything load-bearing into D32's
successors in the shared decision log.

---

## A-share EOD bars — Tiingo (CN-M3)

Design: `2026-08-15-shanghai-briefs-design.md`. All five are answered by one
`tiingo-cn-probe` run (plus one run at ~15:20 CST for Q4); until then CN-M1/M2
run on the synthetic seam and every brief carries the `cn_bars.synthetic`
disclosure.

### CN-Q1 — Does Tiingo's free tier serve SSE/SZSE tickers at all, and in what format?

**Blocks:** CN-M3 live swap · **Status:** answered 2026-08-16 · **Ask first**

Tiingo advertises Chinese equity coverage, but the free-tier entitlement and
the ticker format (`600519-SS`? `600519-SHG`? bare `600519`?) are undocumented
guesses. `_vendor_symbol()` is written against a placeholder mapping until the
probe reports which format resolves.

*If unavailable:* the synthetic seam stays; escalate down the fallback table —
Stooq (keyless, `.ss`/`.sz`), AkShare (free, scraping fragility), EODHD
(~$20/mo, confirmed coverage). The provider seam makes this a constructor
change.

### CN-Q2 — Is `adjClose` real for A-shares?

**Blocks:** CN-M3 · **Status:** answered (partial) 2026-08-16

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

**Blocks:** the 15:20 CST close-brief send time · **Status:** open (bar-existence check done 2026-08-16; same-day-timing half still open) · **High priority**

The CN close brief polls for today's bar before assembling (D20 semantics: a
missing bar fails loudly, never a stale send). If Tiingo publishes A-share EOD
hours after the close, a 15:20 send can never carry same-day bars.

*Test:* run the probe at ~15:20 CST on a session day and check whether today's
bar exists.
*If late:* renegotiate the send time (`CN_SEND_DELAY_MINUTES` — e.g. 17:30
CST, or a T+1-morning close brief). A config change, not code.

### CN-Q5 — Is CSI 300 servable as an index (`000300`), or only via the `510300` ETF?

**Blocks:** CN benchmark line · **Status:** answered (negative) 2026-08-16

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

### CN-Q1 — Does Tiingo's free tier serve SSE/SZSE tickers at all, and in what format?

**Answered 2026-08-16**, live `tiingo-cn-probe` run (`uv run -m worker.cli
tiingo-cn-probe`) at 2026-08-16T01:18:28Z (09:18 CST, a Sunday). **Yes, on the
bare numeric code** (`600519`, `300750`) — not `600519-SHG`/`300750-SHE`
(the original guess), not `600519.SS`/`300750.SZ` passthrough, not
`600519-SS`/`300750-SZ`. All three non-bare-code candidates 404'd for both
held-name samples. `CN_TIINGO_FORMATS` (`worker/providers/tiingo.py`) updated
to `{".SS": "{code}", ".SZ": "{code}"}`. 10 records returned for both symbols
over the 10-session probe window (2026-08-03..2026-08-14).

*Still open:* only two held-name samples were probed (one SSE, one SZSE) plus
the `510300.SS` benchmark ETF fallback (also resolves on the bare code, see
CN-Q5) — broader held-book coverage isn't verified yet. CN-Q3 (rate limits)
is unaffected by this answer and stays open.

### CN-Q2 — Is `adjClose` real for A-shares?

**Answered (partial) 2026-08-16**, same probe run. `adjClose` is present on
every returned record for both symbols. Over the Aug 3–14 window: `600519.SS`
(Kweichow Moutai) shows `adjClose == close` on every bar (no corporate action
touched this window — expected, A-share dividends mostly land June–July).
`300750.SZ` (CATL) shows `adjClose != close` on at least one bar in the same
window — positive evidence `adjClose` is a computed field, not `close`
mirrored into it.

*Still open:* this wasn't the brief's originally-suggested test (a name
pulled across a **known** June/July ex-div date, checking the divergence
lands exactly on the ex-date) — it's an incidental observation from a generic
recent window. The specific ex-date-alignment test is not done; treat CN
returns as provisionally trustworthy, not fully verified, until it is.

### CN-Q5 — Is CSI 300 servable as an index (`000300`), or only via the `510300` ETF?

**Answered (negative) 2026-08-16**, same probe run plus a follow-up
`tiingo-cn-probe --symbols 510300.SS`. `000300.SS` (CSI 300 index) 404'd under
all four candidate formats (`000300-SHG`, `000300.SS`, `000300`, `000300-SS`)
— **not servable on Tiingo's free tier under any tried format.** The
documented fallback resolves cleanly: `510300` (bare code, same CN-Q1 format)
returned 10 records, `adjClose` present, no divergence in this window.
`CN_BENCHMARK` should move to `"510300.SS"` when CN-M3 flips `CN_BARS_LIVE`
on; tracking error vs the true index stays a documented caveat, not a
blocker.

### CN-Q4 — How soon after 15:00 CST does the EOD bar publish? (partial)

**Bar-existence check done 2026-08-16**, same probe run, executed
2026-08-16T01:18:28Z = **09:18 CST on a Sunday** (not a session day). Under
`CN.previous_session`, the latest XSHG session as of that instant was Friday
2026-08-14, and the latest bar returned for both symbols was also dated
2026-08-14 — a same-day match, but a **trivial** one: with no more recent
session to lag behind, any run between Friday's close and the next session
would show "same-day" regardless of how many hours after 15:00 CST Friday's
bar actually published. This run carries no information about intraday
EOD-publish latency.

*Still open:* the actual question — same-day availability checked **at
~15:20 CST on a session day**, immediately after that day's own close — is
unanswered. Until it's run at the right wall-clock time, `CN_SEND_DELAY_MINUTES`
stays at its current value and the close job's existing poll-with-timeout
(`ensure_todays_bars`, D20 semantics: a missing bar fails loudly, never a
stale send) is the safety net.
