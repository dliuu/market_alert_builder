# Session Brief

Twice-daily market brief emails — one before the open, one after the close — covering your own stocks and sectors.

Not a trading terminal, not advice. A briefing.

## Docs

| | |
|---|---|
| `docs/01-product.md` | What it is, who for, non-goals |
| `docs/02-architecture.md` | Topology, service boundary, vendors, cost |
| `docs/03-data-model.md` | Schema and metric definitions |
| `docs/04-brief-object.md` | The contract everything renders from |
| `docs/05-content-spec.md` | Section-by-section, and the five mechanisms |
| `docs/06-email.md` | DNS, provider, HTML-email constraints, failure policy |
| `docs/07-decisions.md` | Decision log — read before proposing an alternative |
| `docs/08-milestones.md` | Build order with definitions of done |
| `docs/09-supabase-setup.md` | One-time Supabase project + `.env` setup |
| `design/design-reference.html` | Visual reference — open in a browser |

`CLAUDE.md` is the entry point for Claude Code.

## Status

M0 (scaffold) code-complete: `apps/web` (Next.js 15) and `apps/worker` (Python 3.12)
boot, Alembic runs, and `pnpm contracts:gen` generates both bindings from the schema.
Pending live Supabase connection — see `docs/09-supabase-setup.md`.
