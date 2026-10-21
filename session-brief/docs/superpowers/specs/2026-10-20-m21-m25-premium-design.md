# Premium: implied volatility, the call strip, and the covered-call regime (M21–M25)

**Intent.** Tell the reader, per owned name, whether option premium is rich, fair or cheap *for that name*, and what the calls a covered-call writer would actually choose between pay today and how that changed since yesterday. Surface it first on a dashboard, then in the close brief as a gated section, then as a state-change strip in the open brief. The brief measures and discloses; it never says buy, sell, write or hold.

The long-form design, with the vendor comparison and the live probe findings, is the Claude doc "IV Covered-Call Dashboard Plan" (https://claude.ai/artifact/NdDi7eWfHQkRMinn54vNNq). This file is the repo's copy of what was decided.

## Data source: Cboe, directly

- **Option chain.** `GET https://cdn-api.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json` (the `cdn.cboe.com` host answers 307 to this one). One JSON document per underlying: a `timestamp` (UTC, no zone marker), an underlying block (`symbol`, `current_price`, `close`, `prev_day_close`, OHLC, `volume`, `iv30`, `iv30_change`, `iv30_change_percent`, `last_trade_time`) and an `options` array where every contract carries `option` (OCC symbol), `bid`, `bid_size`, `ask`, `ask_size`, `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`, `theo`, `open_interest`, `volume`, `last_trade_price`, `last_trade_time`, `change`, `percent_change`, `prev_day_close`, OHLC. 15 minutes delayed, no key, undocumented.
- **Units, verified 2026-10-20 on AAPL:** per-contract `iv` is a fraction (`0.2604`); the underlying `iv30` is in vol points (`26.831`); `iv: 0.0` means none (deep ITM). `current_price` drifts in extended hours; `close` holds the 16:00 print, so **moneyness and spot use `close`**. `open_interest` and `volume` arrive as floats.
- **Index level.** `GET …/delayed_quotes/options/_VIX.json` — same shape; its underlying `close` is today's VIX.
- **Index history.** `GET https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{VIX|VIX9D|VIX3M}_History.csv`, header `DATE,OPEN,HIGH,LOW,CLOSE`, dates `MM/DD/YYYY`, from 1990 / 2011 / 2009. **The VIX file lags a session on the evening of the session**; the delayed quote carries the day.
- **Licensing.** Cboe's site terms permit one copy for personal non-commercial use and say the materials are not for trading purposes; they do not address automated access. Single-user, non-redistributed, the same posture as Tiingo's free tier. Licensed fallbacks behind the same seam: Market Data Starter ($30/mo), Cboe DataShop (~$35/symbol-month). Owner's call; recorded in D38.
- **Switch.** Cboe needs no key, so D29's derive-from-the-key rule has no handle. `OPTIONS_LIVE` is a hand-set flag (the `CN_BARS_LIVE` precedent), default false → deterministic `SyntheticOptionsProvider` and `premium.synthetic` in `data_quality.stale`.

## What is stored

Market-data tables, shared, no `user_id` (`docs/03` rule). Alembic `0020_options`.

| Table | Key | Holds |
|---|---|---|
| `option_contracts` | `contract` | `symbol`, `expiration_date`, `put_or_call`, `strike`, `first_seen` — parsed from the OCC symbol |
| `option_snapshot` | `(contract, session_date)` | bid/ask with sizes, last, iv (vol points), delta, gamma, theta, vega, rho, open_interest, volume, prev_day_close, captured_at — **only the contracts the strip and IV30 selected**, ~30 per name per session |
| `option_strip` | `(symbol, session_date, tenor, strike_rule)` | the twelve cells: contract, dte, moneyness, mid, premium_pct, time_value_pct, iv, delta, open_interest, volume, chg_contract_pct, chg_tenor_pct, iv_chg, tenor_collapsed, target_miss |
| `index_daily` | `(symbol, session_date)` | o, h, l, c for VIX, VIX9D, VIX3M |

`raw_payloads` (invariant 5) keeps, per name per session, the underlying block and every option object with ≤ 60 days to expiry and a strike within ±25% of `close`, object-for-object as published (D38: a full large-cap chain is ~1.5 MB; this subset is everything IV30 and the strip can read — 441 KB of 1,448 KB on AAPL). Index history stores only rows past the watermark, not the 470 KB file. `options rebuild` reproduces `option_contracts` and `option_snapshot` from `raw_payloads` with zero network calls; `option_strip` and the metrics are re-derived by the next close compute.

Per-user numbers go to `metrics` under the dev user: `iv30`, `iv30_n`, `iv30_cboe`, `iv30_pctile`, `iv_rank_252`, `hv20`, `hv60`, `iv_hv_spread`, `iv_hv_ratio`, `iv_vix_ratio`, `iv_vix_ratio_pctile`, `vix_pctile`, `iv_term_slope`, `contracts_writable`. Enums (`premium`) are not stored (M19 rule); they are recomputed from metrics by the same pure function.

## The signal

**IV30, constant maturity.** Two expiries bracketing 30 calendar days, the nearer ≥ 7 days out; at each, the two strikes nearest `close` on either side; calls and puts with two-sided quotes (`bid > 0`, `ask > bid`); drop a strike whose call IV and put IV differ by more than `max(3, 0.10 × mean)` vol points; average survivors per expiry; interpolate linearly in total variance (`iv² · T`) to 30 days. `iv30_n` counts contributors; fewer than 4 → `iv30_thin`. `iv30_cboe` is Cboe's own; a drift over 1 point is logged. Live probe: ours within 0.4 points of Cboe's on AAPL, ASTS, F.

**Realized.** `hv20`, `hv60`: annualized sample stdev of close-to-close log returns on `adj_c` over the prior 20/60 sessions, today excluded, × √252, in vol points.

**Three comparisons, each percentiled under M20's rule** (`oscillators.percentile`: strict `<`, baseline = the 252 sessions before today, null below a full baseline):

| Comparison | Metrics |
|---|---|
| vs own year | `iv30_pctile`, `iv_rank_252 = (iv30 − min) / (max − min)` over the same 252 |
| vs realized | `iv_hv_spread = iv30 − hv20`, `iv_hv_ratio = iv30 / hv20` |
| vs the tape | `iv_vix_ratio = iv30 / vix_close`, `iv_vix_ratio_pctile`, `vix_pctile` (VIX against its own 252 from `index_daily`) |

**The call strip.** For tenor in (7, 14, 21, 30) calendar days: the listed expiry nearest the tenor (tie → nearer), `dte ≥ 1`; at it, for rule in `itm10 = 0.90`, `atm = 1.00`, `otm10 = 1.10` × `close`: the two-sided call with the nearest strike (tie → lower). Per cell: `mid = (bid+ask)/2`, `premium_pct = mid/close`, `time_value_pct = (mid − max(close − strike, 0))/close`, `moneyness = strike/close − 1`, `target_miss = |moneyness − (rule − 1)| > 0.025`, `tenor_collapsed` when the expiry was already used by a shorter tenor. Changes against the prior session: `chg_contract_pct` (same contract's mid, from our prior snapshot), `chg_tenor_pct` (same tenor-and-rule cell's `premium_pct`), `iv_chg`. `iv_term_slope = iv(7d atm) − iv(30d atm)`.

**Regime** (`premium`, computed in assembly and on the dashboard by one pure function):

- `rich`: `iv_hv_spread > 0` and (`iv30_pctile ≥ 70`, or `iv30_pctile` is null and `iv_vix_ratio` ≥ the book's median ratio that session)
- `cheap`: `iv_hv_spread < 0` or `iv30_pctile ≤ 30`
- `fair`: otherwise, and always when `iv30_thin`

**Guards, carried as row fields (D16: renderers show, never decide):** `earnings_in_window` (an `events` row of type `earnings` between the session and the later of the IV30 far expiry and session + 30 days; carries the date), `contracts_writable = floor(open-lot shares / 100)`, `iv30_thin`, and `premium.stale` / `premium.synthetic` in `data_quality.stale`.

**Thresholds** live in `constants.py` under `# --- Premium (M22) ---`, expected to move once: 70/30, 3 points / 10%, 4 contracts, 7-day floor, 2.5% strike miss, 1-point drift alarm, 60-day / ±25% storage band.

## Surfaces

**Dashboard (M23), `/volatility` in `apps/web`.** Server component, Drizzle read-only, no vendor calls. Market line (VIX close, its percentile, VIX9D, VIX3M, session date, Cboe attribution). Book table, one row per holding (watching names marked): close, IV30, Cboe IV30, IV30 pctile, IV rank, HV20, IV−HV, IV/VIX, term slope, regime, earnings-in-window date, contracts writable, thin, stale; sorted by IV30 pctile desc, nulls last. Per name on expand: the 4×3 strip grid (dte/expiry per row, strike/moneyness per column; each cell mid, prem%, tv%, IV, delta, OI, both changes; misses and collapses marked) and an IV30-vs-HV20 history over the trailing year with earnings dates marked. Method footer: today's IV30 contributors and the parity drops, ours vs Cboe's, capture time. Regime is a word with a muted colour, not a traffic light.

**Close brief (M24), new §6 "Premium" between §5 Where they stand and Sector rotation.** `schema_version` 10, section id `premium`. Row per name with: `iv30`, `iv30_pctile`, `iv_rank_252`, `hv20`, `iv_hv_spread`, `iv_vix_ratio`, `vix_pctile`, `iv_term_slope`, `premium`, `earnings_in_window`, `occurs_at` (the earnings date), `contracts_writable`, `iv30_thin`, and the strip line `call_atm30_pct`, `call_atm30_chg`, `call_otm30_pct`, `call_otm30_chg`, `call_atm7_pct` (the 7-day cell only when the slope is positive). Gating, the §5 mechanism: a name qualifies when `premium ∈ {rich, cheap}`, not `earnings_in_window`, not `iv30_thin`, `contracts_writable ≥ 1`; ordered by `iv30_pctile` desc (nulls last) then `|iv_hv_spread|`; capped at three via `row.tier` (`full` / `brief`), overflow named in the note; emitted even when empty ("Premium is fair across the book"); absent entirely when never computed (CN). Email shows `full` rows; the archive shows all. `iv30` is never rendered in email without `iv30_pctile` (M20 rule); the archive may show it bare, marked. Narration may write the section note (`premium_note`) under the digit guard and must characterise, never recommend; a fixed assembled sentence is the fallback. No `why` per row. No claims in v1.

**Open brief (M25).** Section id `premium` on the open kind: rows only for names whose regime differs from the prior session (`premium`, `premium_prior`), omitted when none; no new ingest at 08:00 (reads the prior close's metrics and recomputes both regimes with the pure function). The §2 `^VIX` tape row gains `level_pctile` (full-baseline only).

## Non-goals and anti-goals

- No action verbs anywhere: not in templates, not in the narration prompt, not in notes. The section comes out if one ever appears (D37 reversal clause).
- No Black-Scholes inversion in v1; Cboe's IV is used as published.
- No claims, no `why` lines, no CN book, no VIX futures curve, no skew.
- No DataShop backfill in v1; percentiles stay null until the baseline fills.

## Decisions to record

- **D37** — Premium regime is a measurement, not advice: amends `docs/01` *not advice* under four conditions (mechanical thresholds; every number carries its percentile or comparison base; the earnings guard is always visible; no action verb in template or prose). Reversal: an action verb appearing anywhere removes the section until fixed.
- **D38** — Cboe directly, undocumented, and the raw-payload band: why the exchange's own delayed feed over a paid API, the terms as read, the 60-day / ±25% storage subset as the one place invariant 5 bends, and the licensed fallbacks.

## Milestones (definition of done, `docs/08` entries)

- **M21 — Cboe options feed.** `OptionsProvider` protocol, `CboeClient` + `SyntheticOptionsProvider`, `0020_options`, `options_ingest.pull_options` / `rebuild_options`, `options probe|pull|rebuild` CLI, `OPTIONS_LIVE`. *Done when:* `options probe` resolves every held name's chain and the three index files; `options pull` stores the band subset and the selected snapshots; `options rebuild` reproduces `option_contracts` and `option_snapshot` byte-for-byte with zero network calls (the strip and metrics follow from the next compute); the suite passes in synthetic mode.
- **M22 — Volatility compute.** `worker/volatility.py` (strip, IV30, HV, percentiles, VIX ratios, regime, guards), `compute_and_store_volatility`, the close job's options pull between bars and assemble, metric keys in `docs/03`. *Done when:* a hand-computed IV30, strip cell and percentile match stored rows for one seeded name; a thin chain yields a flagged value that never qualifies; a short history yields null percentiles; the close job still sends when the pull fails, stamped stale.
- **M23 — Dashboard.** `/volatility`. *Done when:* the page renders the book from the ephemeral DB with the strip grid and history per name, `pnpm --filter web typecheck` and `pnpm check` pass.
- **M24 — Close brief §6.** v10, `_premium`, email + archive, narration note, D37/D38, docs 01/04/05/07/08. *Done when:* Gmail/Outlook render under 80KB, a stored v9 brief renders unchanged, `pnpm contracts:gen` is clean, a name with earnings in the window never qualifies.
- **M25 — Open brief strip.** *Done when:* a session with no regime change renders no strip; the VIX row carries its percentile only with a full baseline.
