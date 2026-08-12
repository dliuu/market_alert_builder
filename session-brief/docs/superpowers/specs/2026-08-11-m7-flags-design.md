# M7 — Flags: position risk + correlation

**Milestone (docs/08):** *Done when:* thresholds fire correctly on a synthetic
fixture and the weekly rate limit holds across three consecutive briefs.

## Scope decisions

- **Synthetic data only.** M7 adds the `fundamentals`, `events`, and `flags`
  tables so synthetic rows can be seeded, builds the pure threshold stage over
  them, and computes correlation from `bars_daily` we already have. Real
  Tiingo/EDGAR earnings & fundamentals ingest is deferred (docs/02 marks these
  TBD source gaps). Matches the DoD, which only requires firing on a *fixture*.
- **Exercised through the close brief.** The flags' real home is the open brief
  §6 exposure_check, which isn't built yet. Per the M6 precedent (D16b), M7
  builds the engine and exercises it by populating the object's existing
  top-level `flags[]` (currently emitted as `[]`) on the close brief. **No
  schema bump** — the contract already defines all seven flag types with
  `severity`/`value`/`text_key`.

## Mechanisms & thresholds (docs/05)

**Position risk** — fires per held name; not weekly-capped (intermittent):
| Flag `type` | Fires when | severity | text_key |
|---|---|---|---|
| `runway` | `cash / mean(burn,4q) < 6` quarters | warn | `runway_low` |
| `dilution` | `shares_out / shares_1y_ago − 1 > 0.15` | warn | `dilution_high` |
| `earnings_soon` | `next_earnings_date` within the proximity window | info | `earnings_soon` |
| `supply_event` | a `lockup` event within 7 days | warn | `supply_event_soon` |
| `short_interest` | `> 0.20` of float | warn | `short_interest_high` |

`short_interest` has **no live source** (docs/02 skips it in v1); the threshold
exists and fires on synthetic input, but nothing feeds it in normal operation.

**Correlation flag** — the exposure mechanism; **weekly-capped** (docs/05):
| Flag `type` | Fires when | severity | text_key |
|---|---|---|---|
| `concentration` | any single name `> 20%` of book | warn | `single_name_concentration` |
| `concentration` | any sector `> 50%` of book | warn | `sector_concentration` |
| `correlation` | mean 20d pairwise correlation `> 0.75` | warn | `corr_20d_high` |

## Design

Mirrors `claims.py`/`tape.py`: a **pure core** + a **DB layer**, kept out of the
M3-certified P&L compute.

- **`worker/flags.py`**
  - Pure threshold predicates over scalar inputs → the DoD's "thresholds fire on
    a synthetic fixture" is a set of trivial unit tests.
  - Pure helpers: `runway_quarters(cash, burns)`, `dilution_yoy(now, year_ago)`,
    `concentration(weights, sector_weights)`, `mean_pairwise_corr(returns)`
    (Pearson; float, off the money path — correlation needs `sqrt`).
  - DB layer: read `fundamentals`/`events`/trailing returns, build candidate
    flags, apply the rate limit against the `flags` table, return the surfaced
    flag dicts, and `record_flags(...)` to upsert `first_seen`/`last_seen`.

- **Rate limit (docs/03: `flags.last_seen`).** A row per
  `(user_id, flag_type, symbol, sector_id)`. `first_seen` = first session
  surfaced; `last_seen` = last session surfaced ("when did I last warn myself").
  Capped types (`concentration`, `correlation`) surface iff there is no row **or**
  `session_date − last_seen ≥ 7 days`; on surfacing, `last_seen` is bumped.
  Uncapped position-risk types surface every send. `last_seen` is only written
  when the brief actually **sends** (a mention), so a skipped quiet session
  spends no rate-limit budget.

- **`assemble()`** gains a `flags: list[dict] | None = None` parameter (stays
  pure; default `[]` preserves the M4/M5 snapshot tests). `assemble_and_store`
  builds candidates → decides surfaced set (read-only) → assembles → and, only
  when the brief sends, stores the brief and `record_flags(...)`.

- **Migration `0007_flags`** creates `fundamentals` (no `user_id` — shared market
  data, PK `(symbol, as_of)`), `events` (no `user_id`), and `flags` (`user_id`,
  RLS, `UNIQUE NULLS NOT DISTINCT (user_id, flag_type, symbol, sector_id)`;
  Postgres 16 per docs/02).

## Earnings proximity caveat

"Within 5 sessions" needs a trading calendar for a *future* date; `bars_daily`
(the claims session-clock trick) has no future bars, and `exchange_calendars`
isn't a worker dependency. M7 thresholds earnings proximity in **calendar days**
(`≤ 7`, the ~5-session band) and keeps the pure predicate scalar-typed, so
swapping to true session-counting when the calendar lands (M10) is a one-line
DB-layer change. No holiday list is hardcoded (invariant 7 intact).

## Testing

- **Pure (`test_flags.py`):** each threshold fires at/over its boundary and stays
  silent under it; `runway`/`dilution`/`concentration`/`mean_pairwise_corr`
  math on hand-checkable inputs; correlation returns `None` under <2 series or
  zero variance.
- **DB (`test_flags_db.py`):** synthetic fundamentals/events → position-risk
  flags appear on the object; **three consecutive close briefs with a persistent
  concentration condition → the flag is mentioned in brief 1 only** (rate limit),
  and reappears after ≥7 days.
- Existing `test_assemble.py` snapshot stays green (flags default to `[]`).

## Out of scope (deferred)

Real earnings/fundamentals/short-interest ingest; the open brief §6 renderer;
persisting `corr_20d` to `metrics` (the flag `value` carries it for now); flag
lifecycle when a condition clears.
