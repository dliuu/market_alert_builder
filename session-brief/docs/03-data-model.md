# 03 — Data model

Postgres 16. Alembic migrations, owned by `apps/worker`. Every table carries `user_id`.

## Book

```sql
users        (id, email, tz, created_at)
sectors      (id, user_id, name, benchmark_symbol, sort_order)
holdings     (id, user_id, sector_id, symbol, status)   -- status: owned | watching
lots         (id, user_id, holding_id, shares, cost_basis_cents, opened_on, closed_on)
```

Use `lots`, not an average cost on `holdings`. Positions get added to, and migrating later is annoying.

- Total P&L = `Σ open lots: shares × (last − cost_basis)`
- Day P&L = `Σ open lots: shares × (close − prev_close)`

## Market data

```sql
raw_payloads (id, source, endpoint, symbol, as_of, body jsonb, fetched_at)
             -- UNIQUE (source, endpoint, symbol, as_of)
bars_daily   (symbol, session_date, o, h, l, c, v, adj_c)   -- PK (symbol, session_date)
quotes       (symbol, session_date, captured_at, last, prev_close, extended_last, extended_v)
             -- PK (symbol, session_date); one pre-open capture per symbol per session (M15)
fundamentals (symbol, as_of, cash_cents, quarterly_burn_cents, shares_out, next_earnings_date)
events       (id, symbol, event_type, occurs_at, payload jsonb)  -- earnings | lockup | macro
news_items   (id, symbol, headline, url, published_at, source)
```

Market data tables have **no `user_id`** — they're shared across the tenant base and keyed by symbol. This is what makes ingest cost scale with the symbol universe rather than the user count.

`attribution (symbol, trade_date, model_version, market_bps, theme_bps, resid_bps, total_bps, resid_z, provisional, …)` (M11/M12) is the same shape: shared, no `user_id`, keyed by `(symbol, trade_date, model_version)`. Assembly (M13) reads it filtered to held names for the session — one query, no per-user compute.

### Catalysts (M17/M18)

```sql
catalyst_insider_tx        (id, symbol, insider_name, insider_title, transaction_date,
                            filing_date, transaction_type, shares, price_cents,
                            value_cents, shares_after, natural_key UNIQUE, ingested_at)
catalyst_proposed_sales    (id, symbol, insider_name, filing_date, shares_proposed,
                            approx_sale_date, broker, natural_key UNIQUE, ingested_at)
catalyst_index_constituents (index_symbol, snapshot_date, symbol, weight, ingested_at)
                            -- PK (index_symbol, snapshot_date, symbol)
catalyst_etf_holdings      (etf_symbol, snapshot_date, symbol, weight, shares, ingested_at)
                            -- PK (etf_symbol, snapshot_date, symbol); INDEX (symbol, snapshot_date DESC)
catalyst_ipos              (symbol PK, listing_date, ingested_at)
catalyst_watermarks        (source, symbol, last_success_at, last_seen_date, seen_keys,
                            last_error, consecutive_fails)   -- PK (source, symbol)
catalyst_signals           (id, source, symbol, kind, ref_date, severity, detail jsonb,
                            member_ids, model_version, computed_at)
                            -- UNIQUE (source, symbol, kind, ref_date, model_version)
catalyst_reporting_state   (user_id, source, symbol, kind, ref_date, first_reported_at,
                            last_reported_at, report_count, max_severity_seen)
                            -- PK (user_id, source, symbol, kind, ref_date)
```

Same shape as the rest of this section: **shared, no `user_id`**, keyed by symbol —
insider filings and index membership are facts about a symbol, not a book.

**`catalyst_reporting_state` is the one exception and does carry `user_id`.** It
holds the report-once decay curve (full → condensed → suppressed, re-escalating
on a severity increase), which is a property of a *reader's* attention rather
than of the signal — sharing one curve would open user #2's first brief at
"condensed" because user #1 had already read it. It is keyed on the signal's
**natural identity**, not `catalyst_signals.id`, because a rebuild reassigns that
id; and it is never dropped when signals are rebuilt (D30). If it were, every
stale cluster would resurface at full volume on the next brief.

`catalyst_signals` is one table across all six sources — the source spec's six
per-source tables plus a `UNION ALL` view, collapsed into the single row shape
that view produced anyway (D30). The typed per-source fields live in `detail`,
exactly as `flags` carries nine heterogeneous types in one `payload jsonb`.
`member_ids` points back to the raw rows that produced a signal: the audit trail
for drill-down and for debugging false positives.

Detectors read only these tables — a signal rebuild costs **zero API calls**,
and the raw tables themselves replay from `raw_payloads`.

`index_events` (from `0010`, read by `exclusions.py` and `concordance.py` and
empty until now) is **populated by M18's index differ** rather than curated by
hand. Reconstitution days become real contaminated-day fit exclusions for
attribution.

**Store raw payloads verbatim, never transform on ingest.** When a vendor changes a field or you find a bug in the RVOL math, you replay from `raw_payloads` instead of re-buying history. A few hundred KB a day.

`quotes` holds the pre-open capture the open brief's §2/§3 read: held names in `extended_last`/`extended_v` (pre-market print, summed pre-market volume), macro tape symbols in `last`/`prev_close`. It is keyed by session rather than by capture timestamp — every read is "the capture for session D", and the session key is what makes re-seeding idempotent.

## Derived

```sql
metrics  (user_id, symbol, session_date, metric, value)   -- PK (user_id, symbol, session_date, metric)
flags    (id, user_id, flag_type, symbol, sector_id, first_seen, last_seen,
          severity, payload jsonb)
         -- flag_type CHECK: concentration | correlation | runway | dilution |
         --   earnings_soon | supply_event | short_interest | theme_misfit |
         --   beta_instability. The last two (M13 Task 7, docs/07 D24) are
         --   dashboard-only: written straight to this table from
         --   attribution_fits/attribution_signals, never into a BriefObject.
claims   (id, user_id, brief_id, symbol, claim_type, direction, horizon_sessions,
          resolved_at, outcome, graded_model_version)  -- outcome: correct | wrong | unresolved
briefs   (id, user_id, session_date, kind, schema_version, body jsonb, created_at)
jobs     (id, user_id, job_type, payload jsonb, status, locked_at, attempts, created_at)
deliveries (id, user_id, brief_id, channel, recipient, status, provider_msg_id,
            attempts, sent_at)
            -- UNIQUE (brief_id, recipient)
```

`metrics` in long format looks wasteful and isn't: it lets you add a metric without a migration, and rolling-window queries stay clean. It carries `user_id` (D14) — contribution, weight and P&L are book-specific, so a metric is only meaningful within a user's book. Units ride the metric name: `*_cents` are integer cents, `*_bps` are basis points, `day_return`/`weight` are fractions.

`flags.last_seen` is what enforces the once-a-week rate limit on the correlation flag. Rate limiting belongs in the data, not the renderer — otherwise you can't answer "when did I last warn myself about this?"

`UNIQUE (brief_id, recipient)` is what stops a crashed worker's retry from mailing the same brief four times.

`claims.graded_model_version` (M13, nullable) stamps the `attribution.model_version` a claim was graded against — mirrors the attribution table's own versioning (D21) so a future model re-spec never silently rewrites past grades.

## Metric definitions

| Metric key | Formula | Inputs |
|---|---|---|
| `day_return` | `(c − prev_c) / prev_c` | daily bars |
| `contribution_bps` | `position_value_Δ / prior_book_value × 10000` | daily bars + lots |
| `total_pnl` | `Σ shares × (c − cost_basis)` | daily bars + lots |
| `rvol` | `v / mean(v, 30d)` | daily bars |
| `range_position` | `(c − l) / (h − l)` | daily bars |
| `rel_strength_1d/5d/20d` | `sym_return − benchmark_return` | daily bars |
| `sector_breadth` | `count(members up) / count(members)` | daily bars |
| `corr_20d` | rolling 20d pairwise on sector daily returns | daily bars |
| `weight` | `position_value / book_value` | daily bars + lots |
| `hhi` | `Σ weightᵢ²` | daily bars + lots |
| `cash_runway_q` | `cash / mean(quarterly_burn, 4q)` | fundamentals |
| `dilution_yoy` | `shares_out / shares_out_1y_ago − 1` | fundamentals |

Everything above runs on **daily OHLCV**. Only gap-fill behaviour and VWAP need minute bars — defer them, and the whole v1 stays on a free data tier.
