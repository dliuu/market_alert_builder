# 05 — Content spec

Section order is fixed. `design/design-reference.html` is the visual authority.

## Open brief (08:15 ET)

| # | Section | Contents |
|---|---|---|
| 1 | The one thing | Single highlighted paragraph. The most consequential fact of the morning. |
| 2 | Overnight tape | ES/NQ futures, 10Y, DXY, VIX, WTI, plus foreign proxies relevant to held sectors. One "read" paragraph. |
| 3 | Your names, pre-market | Only names moving >1% pre-market or carrying news. Pre %, gap in dollars, pre-market volume as a multiple. Each with a `why` line. |
| 4 | On the clock today | Timed calendar: macro releases, earnings, lockups, ex-div. Tagged `macro` / `your holding` / `watchlist`. |
| 5 | Sector setup | Per sector: benchmark 5d, vs SPY 5d, pre-market. |
| 6 | Exposure check | Position risk + correlation flag. Only when thresholds fire. |

No performance, no P&L, no tape quality. Pre-market volume is too thin for RVOL to mean anything.

## Close brief (16:45 ET)

| # | Section | Contents |
|---|---|---|
| 1 | The one thing | Single highlighted paragraph explaining the session. |
| 2 | Session scorecard | Day %, day $, vs SPY in bps, book value, unrealised P&L. |
| 3 | Attribution | Per name: close, day %, day P&L, contribution bps, total P&L. **Plus a book totals row.** |
| 4 | How they traded | Technical snapshot, every owned name: RVOL, weekly/monthly volume multiples, close-in-range bar, distance from the 20/50/200-day averages and their stack, nearest support and resistance with touch counts, the 52-week range, and a confirmed breakout marker (M19). *`vs sector` is still unbuilt — `rel_strength` remains a declared, unpopulated field.* |
| 5 | Sector rotation | Per sector: day move, breadth (`n of m up`), bar vs benchmark. |
| 6 | After hours | Earnings prints and extended-hours moves, with the implied effect on tomorrow's open in bps. |
| 7 | Yesterday's flag, resolved | The accountability loop. Always last. |

## The five mechanisms

| Mechanism | Computed | Stored | Surfaces |
|---|---|---|---|
| Position risk | Stage ③, thresholded in ④ | `fundamentals`, `flags` | Open brief, §6 |
| Correlation flag | ③ rolling window; ④ threshold + rate limit | `flags` (`last_seen`) | Open brief §6 + dashboard, **max 1×/week** |
| Accountability loop | ④ emits; the *next* brief's ④ resolves | `claims` | Close brief, §7 |
| Suppression | ④ sets `section.tier` | `briefs.body.suppressed` | By absence + the roll-up line |
| Tape quality | ③ from daily OHLCV | `metrics` | Close brief, §4 |

### Thresholds

**Position risk** — silent unless: runway < 6 quarters, dilution > 15% YoY, earnings within 5 sessions, short interest > 20% of float, or a known supply event within 7 days. Supply events inside 7 days also get a line in §4 of the open brief.

**Correlation flag** — fires on: mean 20d pairwise correlation > 0.75, any single name > 20% of book, or any sector > 50%. Hard-capped at one email mention per week even when the condition persists. It lives permanently on the dashboard.

**Accountability loop** — claim types are narrow enough to resolve mechanically: `catalyst_pending`, `relative_strength`, `supply_overhang`, `breadth`. Every fourth Friday, the close brief adds a scorecard by claim type.

**Suppression** — three tiers:
- *full* (row + `why` line): moved >1%, or RVOL >1.5×, or carries news
- *brief* (bare row): moved 0.3–1%, **or is >15% of book value**
- *suppressed*: folded into one line — "SERV, MU, TSLA unchanged"

Named exception: anything with earnings inside 5 sessions always gets *full* regardless of movement.

**Weight floor** — a position over 15% of book value is never suppressed, however quiet. At that size "it didn't move" is itself worth seeing, and a name folded into the roll-up line is one you can't reconsider. It earns a *bare row only*: promoting a flat day to *full* would hand it a tape row, make it claim-eligible, and defeat the quiet-session skip below.

Whole-brief version: the close brief is skipped entirely when nothing moved >1%. **The open brief always sends** — "nothing happened overnight" is information you need before the bell.

**Tape quality** — RVOL vs the 30-day average *at the same time of day*, close position in the day's range as a 0–100 percentile rendered as a bar, and gap behaviour. Gap-fill and VWAP need minute bars; defer them.

## Editorial principle

Every mechanism has a threshold that keeps it **silent by default**, a **fixed home** in exactly one of the two emails, and a **permanent copy on the dashboard** for on-demand reading. That's what stops the brief growing into an unread wall over six months.
