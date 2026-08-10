---
name: verify-numbers
description: Audit the numeric integrity of the brief pipeline. Use when changing compute, assembly, P&L, contribution, or the BriefObject, or when a brief's figures look wrong.
---

# Verify numbers

Numeric correctness is the one thing this product cannot get wrong quietly. Run this after any change to `apps/worker/compute/`, `apps/worker/assemble/`, or the lots/holdings schema.

## Checks

1. **Contribution closes.** `Σ rows.contribution_bps == book.day_bps`, within 1bp of rounding drift. If it doesn't, stop — everything downstream is wrong.
2. **P&L reconciles.** `book.day_pnl_cents == Σ rows.day_pnl_cents` exactly. Integer cents, no tolerance.
3. **Total P&L is lot-derived.** `Σ open lots: shares × (close − cost_basis)`. Closed lots must be excluded.
4. **No floats in money.** Grep the diff for float arithmetic on any `*_cents` field.
5. **Range position is bounded.** `0 ≤ range_position ≤ 1`. A value outside that means `h == l` wasn't handled.
6. **RVOL denominator excludes today.** The 30-day average must not include the session being measured.
7. **Benchmark alignment.** Relative strength compares the same session dates. A missing benchmark bar must yield `null`, not zero.

## How

Prefer a property test over a one-off script:

```
uv run pytest tests/test_invariants.py -q
```

If a check fails, write the failing case as a fixture in `tests/fixtures/` before fixing it.

## Report

State each check as pass/fail with the actual numbers. Do not summarise as "looks correct" — show the sums.
