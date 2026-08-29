# M20 manual test runbook — §5 "Where they stand"

Nine checks, run by hand against the real book and a real inbox. Each has an
exact command and an exact expected observation — "verify it looks right" is
not a check. Run them in order; several (4, 5) mutate a constant and must be
restored before the next check runs.

`sess` below is a recent trading session with a close brief already assembled
— substitute a real `YYYY-MM-DD`. Run worker commands from `apps/worker`.

---

## 1. Backfill deep enough

§5 needs 286 sessions per symbol (see D36) — every holding **and** every
sector benchmark, or `rel_strength` renders blank for the whole sector.

```bash
uv run -m worker.cli backfill --days 500
```

Then, against the worker's Postgres:

```sql
SELECT symbol, count(*) AS n
FROM bars_daily
WHERE adj_h IS NOT NULL
GROUP BY symbol
ORDER BY n;
```

**Expected:** every symbol in the book — holdings and every distinct
`sectors.benchmark_symbol` (SPY included) — shows `n >= 286`.

**Known state to call out explicitly:** as of this writing the dev database's
SPY sits at **285** rows, one short of the threshold. If SPY still shows 285
here, `rel_strength`/`rel_strength_benchmark` will render `—` for every
holding in check 2, and that is the backfill not having gone deep enough yet
— not a bug in assembly. Re-run the backfill and re-check the count before
treating check 2's blank relative-strength column as a finding.

---

## 2. Produce a brief

```bash
uv run -m worker.cli brief --kind close --date <sess> --dry-run
```

**Expected:** the printed brief includes a `standing` section with **0 to 3**
blocks (never more — the cap is 3). If SPY is still short per check 1,
`rel_strength` renders `—` throughout but the section itself still renders
normally off the other four indicators.

---

## 3. Audit one percentile by hand

Pick one rendered `RSI` percentile from check 2's output, for symbol `SYM` on
session `sess`. This is the check that matters most — M19's first touch
predicate looked correct and scored ASTS at 61 touches in 276 sessions on
real data, and only a real-book read caught it. A percentile mechanism is the
same kind of "looks right, isn't" risk.

`metrics` is not a usable source for this: `compute_and_store_standing` runs
once per assembled brief and gains one `rsi14` row per session going
forward, and check 2 ran with `--dry-run`, which rolls its transaction back
and stores nothing at all — on day one this table has 0-1 rows for `SYM`,
not 253. Percentile history is deliberately not stored (`docs/03`); it
recomputes from `bars_daily` on every run, so the audit has to as well. Pull
`adj_c` directly and recompute the RSI series the way `worker/oscillators.py`
does (Wilder's, SMA-seeded), then rank today against the 252 prior values —
run from `apps/worker`:

```bash
uv run python -c "
from worker.db import get_engine
from worker.oscillators import rsi_series, percentile, READ_DEPTH
from sqlalchemy import text
from decimal import Decimal

SYM, SESS = 'SYM', '<sess>'
with get_engine().connect() as conn:
    rows = conn.execute(text('''
        SELECT adj_c FROM bars_daily
        WHERE symbol = :sym AND session_date <= :sess AND adj_c IS NOT NULL
        ORDER BY session_date DESC LIMIT :depth
    '''), {'sym': SYM, 'sess': SESS, 'depth': READ_DEPTH}).all()

closes = [Decimal(str(r[0])) for r in reversed(rows)]  # oldest -> newest
rsi = rsi_series(closes)
print('rsi14 today:', rsi[-1] if rsi else None)
print('pctile:', percentile(rsi))
"
```

**Expected:** the printed `pctile` matches the `rsi14_pctile` value printed
in check 2's output for `SYM`, to within rounding. If `rows` comes back
shorter than 253 sessions, `percentile()` returns `None` by design — that
means `SYM` correctly should **not** have shown a percentile in check 2
(confirm it rendered `—` there); it is not a bug to report.

---

## 4. Force the gate open

`worker/assemble.py` gates §5 on `_STRETCHED_HIGH = Decimal("90")` /
`_STRETCHED_LOW = Decimal("10")`.

1. Temporarily edit `worker/assemble.py`: change `_STRETCHED_HIGH = Decimal("90")`
   to `_STRETCHED_HIGH = Decimal("50")`.
2. Re-run: `uv run -m worker.cli brief --kind close --date <sess> --dry-run`.
3. **Expected:** more names qualify than in check 2 (loosening the gate can
   only add names, never remove them). If more than 3 qualify, the section's
   `note` names every qualifying symbol beyond the three shown.
4. **Restore** `_STRETCHED_HIGH = Decimal("90")` and re-run once more to
   confirm the brief matches check 2's output again before moving on.

---

## 5. Force the empty state

1. Temporarily edit `worker/assemble.py`: change `_STRETCHED_HIGH = Decimal("90")`
   to `_STRETCHED_HIGH = Decimal("100")` (and, if any name still qualifies via
   divergence or a §4 breakout, note that those two gate arms are independent
   of this constant and may still admit a name — check 2's known-good output
   tells you what to expect here).
2. Re-run: `uv run -m worker.cli brief --kind close --date <sess> --dry-run`.
3. **Expected:** the `standing` section renders with **zero rows** and its
   `note` reads `"No name cleared the decile"` (unless a divergence or
   breakout still qualifies a name, per the caveat above).
4. **Restore** `_STRETCHED_HIGH = Decimal("90")` and re-run to confirm the
   brief matches check 2's output again.

---

## 6. Short-history honesty

Add a symbol to the book with roughly 90 days of price history (well short of
the 286-session read depth) — either a genuinely new holding, or a synthetic
row set via a short backfill window for a scratch symbol.

```bash
uv run -m worker.cli backfill --symbols <SYM> --days 90
uv run -m worker.cli brief --kind close --date <sess> --dry-run
```

**Expected:** the short-history symbol's §5 row, in the dry-run's full
`standing.rows`, carries populated `rsi14`/`macd_hist`/`adx14`/`atr_pct`
values with `null` for every one of their `*_pctile` fields — the value is in
the object. Neither renderer shows it that way: the web archive and the
email both render `—` for those fields rather than the bare value, because a
value is never shown without its percentile. The symbol never appears among
the email's capped/qualifying blocks either — a short baseline cannot clear
the gate because the gate reads only the percentile fields, which are all
null.

---

## 7. Email clients

Send a real close brief to yourself (via the worker's send path or the
`/api/render` endpoint feeding a real send), for a session where §5 has at
least one qualifying name. Open it in:

- Gmail, web
- Gmail, iOS
- Outlook

**Expected:** the percentile bars (`RangeBar`) render as filled two-tone bars
in every client, nothing in §5 is visually clipped or truncated, and the
divergence badge (if present) is legible. Note the rendered size: as of this
writing the close brief renders at **~38KB**, comfortably under both the
project's 80KB budget and Gmail's ~102KB clip threshold — confirm the actual
size for this send is still in that range (Gmail clips the *whole message*
past its threshold, which would silently truncate everything below the cut,
not just §5).

---

## 8. Dark mode

In Apple Mail, toggle system dark mode on and view the same brief from
check 7. `docs/06-email.md` notes the oxblood/pine divergence pair was chosen
partly because it survives inversion better than pure red/green.

**Expected:** if §5 shows a divergence badge, its color remains clearly
bearish-red (oxblood) or bullish-green (pine) — not inverted into the
opposite-looking hue, and not washed out to the point of being unreadable
against the dark background.

---

## 9. §4 unchanged

Diff the rendered §4 ("How they traded") table against a brief for the
**same session**, rendered by the pre-M20 code (a checkout of `main` before
this branch merged, or a stored pre-M20 fixture rendering).

**Expected:** byte-identical §4 output. M20 changes §4's *documentation*
(the stale "`vs sector` unbuilt" clause is removed from `docs/05`) but never
its markup or its data — `_tape_quality`/`Tape` are untouched by this
milestone.
