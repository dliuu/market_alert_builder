# Market Alert Builder — Session Brief

Twice-daily market brief emails — an **open brief** before the bell (08:15 ET) and a
**close brief** after it (session close + 45 min) — covering your own stocks and
sectors. The email is the product; the web app is input, archive, and template
rendering. Not a trading terminal, not advice. A briefing.

The application lives in [`session-brief/`](session-brief/) (this repo root wraps it
with CI and tooling). Start with
[`session-brief/README.md`](session-brief/README.md) and the docs it indexes;
`session-brief/CLAUDE.md` is the entry point for Claude Code.

## What it does

A Python worker runs an unattended pipeline twice per trading day:

```
① ingest → ② normalize → ③ compute → ④ assemble → ⑤ narrate → ⑥ render → ⑦ deliver
```

- **Ingest/compute** — daily EOD bars from Tiingo, P&L/contribution/attribution over
  your book (holdings, lots, sectors entered in the web UI).
- **Assemble** — a versioned `BriefObject` (JSON), the single contract both the email
  and the web archive render from (`packages/contracts/`).
- **Narrate** — Claude writes prose only; every number is computed. Narration failure
  is non-fatal: the brief ships tables-only.
- **Render** — the worker POSTs the brief id to the web app's
  `/api/render/:brief_id` (shared secret) and gets `{ html, text }` back, so the
  React Email template is the only template.
- **Deliver** — Resend, idempotent on `(brief_id, recipient)`.

Alongside the briefs, the scheduler runs three attribution jobs: an AM reconcile
(07:00 ET), a PM provisional score (18:30 ET), and a weekly refit (Sat 08:00 ET).

## Where things run

| Piece | Where | Notes |
|---|---|---|
| `session-brief/apps/worker` | **Fly.io**, app `session-brief-worker`, region `iad` | One always-on `shared-cpu-1x` / 512MB machine running the APScheduler loop (`worker.cli schedule`). No HTTP service. |
| `session-brief/apps/web` | **Vercel** | Next.js 15. Auto-deploys on merge to `main` via the GitHub integration. |
| Database | **Supabase** Postgres 16 | The shared surface between the two services. The worker owns the schema (Alembic only). |
| Email | **Resend** | |
| Narration | **Claude API** | |
| Market data | **Tiingo** (EOD daily bars) | Free tier, personal use only — a commercial plan is the gate to any multi-user product (see `docs/02-architecture.md`). |
| Monitoring | **Healthchecks.io** | Dead-man's switch per scheduled job: the worker pings on every run (including correct holiday skips) and pings `/fail` on a crash. A dead worker stops pinging and the check goes red. |

Inspect the live worker:

```bash
fly status -a session-brief-worker
fly logs   -a session-brief-worker
```

## How to deploy

### Worker (Fly.io) — manual, every time worker code changes

**Merging a PR does not deploy anything.** CI runs tests only; the Fly machine keeps
running whatever image the last `fly deploy` built. After merging changes that touch
`session-brief/apps/worker/` (code, `contracts/`, `alembic/`, `Dockerfile`,
`pyproject.toml`/`uv.lock`):

```bash
git switch main && git pull
cd session-brief/apps/worker
fly deploy
```

The release command runs `alembic upgrade head` before cutover — migrations are
applied automatically, and a failed migration aborts the deploy. Afterwards, confirm
the machine is `started` in `fly status` and that the logs show the scheduler's
startup lines (`scheduler: started; first fire …` plus the refit/pm/am fires).

Secrets are the worker's `.env`, held as Fly secrets (`fly secrets list` to see
names). Set or rotate with:

```bash
fly secrets set NAME=value -a session-brief-worker            # restarts the machine
fly secrets set NAME=value -a session-brief-worker --stage    # applies on next deploy
```

Required: `DATABASE_URL`, `TIINGO_API_KEY`, `RESEND_API_KEY`, `RENDER_SHARED_SECRET`,
`WEB_RENDER_URL`, `BRIEF_FROM`, `BRIEF_RECIPIENT`. Optional but strongly recommended:
`ANTHROPIC_API_KEY` (prose), `HEALTHCHECKS_URL` (close), `HEALTHCHECKS_OPEN_URL`
(open), `HEALTHCHECKS_REFIT_URL` / `HEALTHCHECKS_PM_URL` / `HEALTHCHECKS_AM_URL`
(attribution jobs). See `session-brief/.env.example` for what each is.

### Web (Vercel) — automatic

Merging to `main` deploys the web app. Nothing to do unless the change spans the
BriefObject contract (`packages/contracts/`), in which case deploy in this order:
merge → let Vercel finish → `fly deploy` the worker, so the render endpoint already
understands the new object shape when the worker first sends it.

### Operational gotchas, learned the hard way

- **Fly trial accounts kill machines after 5 minutes.** The account must have active
  billing or the scheduler dies silently shortly after every boot.
- The machine's restart policy is `no`: a crash stops all sends until someone
  intervenes. The Healthchecks pings are how you find out — keep them configured.
- The worker image only knows the code it was built from. If a brief feature "isn't
  sending", check `fly logs` for the startup lines before debugging the code: the
  deployed image may simply predate the feature.

## Local development

```bash
cd session-brief
cp .env.example .env            # fill in (Supabase, Tiingo, Resend, …)
pnpm install && pnpm dev        # web on :3000

cd apps/worker
uv sync
uv run alembic upgrade head
uv run -m worker.cli backfill --symbols SPY,AAPL --days 90
uv run -m worker.cli brief --kind close --dry-run
uv run -m worker.cli schedule --dry-run   # print the next fire times, send nothing
uv run pytest
```

`pnpm contracts:gen` regenerates both contract bindings from the JSON Schema; CI
fails if the committed output drifts.

## Repo layout

```
session-brief/
  apps/web        Next.js 15 — book UI, brief archive, React Email templates, render endpoint
  apps/worker     Python 3.12 — pipeline, scheduler, Alembic migrations (owns the schema)
  packages/contracts  BriefObject JSON Schema → generated TS + Pydantic
  docs/           product, architecture, data model, decisions, milestones
.github/workflows/ci.yml   typecheck · lint · pytest · contracts drift (no CD)
```
