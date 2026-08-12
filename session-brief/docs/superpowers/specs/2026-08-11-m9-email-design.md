# M9 — React Email + Resend + DNS

**Done when:** the close brief lands in your inbox looking like the design
reference, in Gmail *and* Outlook, under 80KB, with a plaintext part.

Scope decisions (2026-08-11): **close template only** (the open brief's
pre-market pipeline isn't built yet); **real DNS** on a domain you own
(`brief.<yourdomain>`); Outlook verified against a fresh outlook.com inbox.

## Architecture

Unchanged from `docs/02` boundary rule #4 — the template lives only in the web
app; the worker calls one pure endpoint.

```
worker `send` cmd
  → read briefs.body, upsert deliveries(status=pending)
  → httpx POST  web /api/render/:brief_id   ─▶  React Email → {html, text}
  → httpx POST  api.resend.com/emails  (from, to, subject, html, text, List-Unsub)
  → deliveries(status=sent, provider_msg_id)
      fallback: render endpoint down → worker sends a minimal plaintext-only part
```

## 1. React Email close template — `apps/web/emails/`

- `theme.ts` — palette + deliberate font-fallback stacks + shared inline styles.
- `close-brief.tsx` — `(BriefObject) → JSX`, table-based (`Section`/`Row`/
  `Column`). Renders from `.mtop` down; the mail-header chrome in the reference
  is preview-only and the subject is a Resend param, not body.
- Sections map off the BriefObject: **one thing** (highlighter → solid `bgcolor`
  fallback), **scorecard** (`book`), **attribution** table + book totals row,
  **how they traded** (range bars as nested `<td bgcolor>`), **roll-up**
  (`suppressed`), **yesterday's flag resolved** (`resolved_claims`), **exposure**
  (`flags`), **data-quality** line, **footer** (unsubscribe + physical-address
  slot).
- docs/06 rules baked in: 600px fixed, all styles inline, **no web fonts**
  (Archivo→Helvetica Neue,Arial; Newsreader→Georgia,serif; IBM Plex
  Mono→ui-monospace,Menlo), pine/oxblood for up/down (survive dark-mode
  inversion).
- **Plaintext is derived from the same component** via
  `render(el, { plainText: true })` — no parallel text template, so the two
  parts can't drift.

## 2. Render endpoint — `apps/web/app/api/render/[briefId]/route.ts`

`POST`, guarded by `RENDER_SHARED_SECRET` (Bearer, constant-time compare). Loads
the brief by uuid → returns `{ html, text }`. Pure: no writes. `401` bad secret,
`404` missing brief.

## 3. `deliveries` table — Alembic `0008_deliveries.py`

`id, user_id (fk), brief_id (fk→briefs), recipient, status
CHECK(pending|sent|failed|bounced|complained), provider_msg_id, error,
created_at, updated_at`, **`UNIQUE (brief_id, recipient)`** (invariant #6), RLS on
`user_id`. Python owns the schema; web needn't introspect it for M9.

## 4. Send pipeline — `apps/worker/worker/deliver.py`

`deliver_brief(conn, brief_id, recipient)`: upsert pending → if already `sent`,
short-circuit (idempotent) → render via endpoint → POST Resend with
`List-Unsubscribe` + `List-Unsubscribe-Post` → `sent` + `provider_msg_id`.
Failure policy (docs/06): endpoint down → minimal worker-side plaintext (subject
+ one-thing + archive link); Resend 5xx → 3× backoff then `failed`.

## 5. CLI — `worker/cli.py`

`send` subcommand: `uv run -m worker.cli send --date 2026-08-11 --kind close
[--to …] [--dry-run]`. `--dry-run` renders and prints byte-size + plaintext,
never hits Resend. Recipient defaults to `BRIEF_RECIPIENT`.

## 6. Config / env

Worker: `RESEND_API_KEY`, `RENDER_SHARED_SECRET`, `WEB_RENDER_URL`, `BRIEF_FROM`,
`BRIEF_RECIPIENT`. Web: `RENDER_SHARED_SECRET` (already reserved). All added to
`.env.example`.

## 7. Deps + tests

- Web: `@react-email/components`, `@react-email/render`; a `react-email` preview
  script; an `email:check` script that renders the frozen `close_brief.json`
  fixture and **fails if HTML ≥ 80KB or the text part is empty** — the
  machine-checkable half of the done-when.
- Worker: `test_deliver.py` (httpx mocked) — idempotency (second call skips),
  pending→sent, endpoint-down → text-only fallback.

## 8. DNS (manual)

Resend → add domain `brief.<yourdomain>` → paste its SPF TXT, 3 DKIM CNAMEs,
DMARC TXT (`p=none`) into DNS. Record shapes documented in the setup output;
live values come from the Resend dashboard.
