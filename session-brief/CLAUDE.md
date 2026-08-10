# Session Brief

Twice-daily market brief emails — one before the open, one after the close — covering a user's own stocks and sectors. The email is the product; the web app is input, archive, and template rendering.

## Repo layout

- `apps/web` — Next.js 15 (App Router, TypeScript). UI, auth, brief archive, email template rendering.
- `apps/worker` — Python 3.12. Ingest, compute, assemble, send. **Owns the database schema.**
- `packages/contracts` — JSON Schema for the BriefObject, plus generated TS types and Pydantic models.
- `design/design-reference.html` — visual reference for both emails and the dashboard. Open it before changing any renderer.

## Core docs

- @docs/02-architecture.md
- @docs/04-brief-object.md

Read on demand (these are deliberately *not* imported — open them when the task touches them):
`docs/01-product.md`, `docs/03-data-model.md`, `docs/05-content-spec.md`, `docs/06-email.md`, `docs/07-decisions.md`, `docs/08-milestones.md`.

## Commands

```bash
pnpm dev                      # Next.js on :3000
pnpm --filter web typecheck
uv run -m worker.cli backfill --symbols SYM,ASTS --days 90
uv run -m worker.cli brief --kind close --date 2026-08-11 --dry-run
uv run pytest
uv run alembic upgrade head
pnpm contracts:gen            # JSON Schema -> TS types + Pydantic models
```

## Invariants

1. **Python owns the schema.** Alembic is the only migration path. The web app never runs DDL and never `CREATE TABLE`s through Drizzle.
2. **The LLM never produces a number.** It writes prose only; every figure is substituted from the BriefObject.
3. **Contribution bps must sum to the book return.** If a change breaks this, stop and fix it before continuing.
4. **Every table has `user_id`**, including in single-user development.
5. **Raw vendor payloads are stored verbatim** in `raw_payloads` and never mutated. Recomputation replays from them.
6. **Sends are idempotent** on `(brief_id, recipient)`. Never bypass the unique constraint.
7. **Trading days come from `exchange_calendars`.** Never hardcode holidays or a 16:00 close.
8. **UTC at rest, `America/New_York` in logic.**

## Conventions

- Python: `ruff`, `mypy --strict`, `pytest`. TypeScript: `biome`, strict `tsconfig`.
- Money is `Decimal` in Python and integer cents at rest. Never float.
- Adding a metric: document in `docs/03-data-model.md`, compute in the worker, expose in the BriefObject, bump `schema_version`, regenerate contracts.
- Prefer a failing test that reproduces the bug over a defensive `try/except`.
