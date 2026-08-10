# 01 — Product

## What it is

Two emails per trading day for an individual investor tracking their own stocks and sectors.

- **Open brief, 08:15 ET** — forward-looking. What changed overnight, what to be ready for. No performance, no P&L.
- **Close brief, 16:45 ET** — backward-looking. What happened, what it cost or made, and whether the morning was right.

Both send outside the bell, so they're read when they can still be acted on.

## Who it's for

One person, initially the author. A serious retail investor with a concentrated, speculative book who wants attribution and discipline, not signals or recommendations.

## Non-goals

- **Not a trading terminal.** No live quotes, no charts, no order entry.
- **Not advice.** The brief never says buy, sell, or hold, and carries no price targets.
- **No intraday alerts.** A third channel would turn a briefing into a monitor, which is a different product with worse habits attached.
- **No technical indicator soup.** RSI/MACD/Bollinger across eight names is noise.

## The two design rules

**Different jobs, different templates.** Most stock newsletters send the same layout twice a day and it reads as filler by the second week.

**Dollars, not just percent.** "ASTS −6.4%" is trivia. "ASTS −6.4%, −$1,193, −75bps of your book" is information. This is why the setup screen asks for share count and cost basis, and it's the single biggest upgrade over anything off the shelf.

## Content spec

See `docs/05-content-spec.md` for the section-by-section breakdown of both emails and the five editorial mechanisms (position risk, correlation flag, accountability loop, suppression, tape quality).

## Visual reference

`design/design-reference.html` — four tabs: setup, open email, close email, dashboard. All figures in it are invented sample data chosen to tie out arithmetically, so it's safe to use as a fixture shape but not as real numbers.
