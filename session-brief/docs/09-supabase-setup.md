# 09 — Supabase setup

The scaffold is wired to read every secret from `.env` (gitignored). Nothing is
connected until you fill it in. This is a one-time, ~5-minute setup.

## 1. Create the project

1. Sign in at [supabase.com](https://supabase.com) → **New project**.
2. Name it `session-brief`, pick a region near you, and set a strong database
   password (save it — you need it in step 2).
3. Wait for provisioning (~2 min).

## 2. Collect the connection strings

**Project Settings → Database → Connection string.**

- **Session pooler** (port `5432`) — used by Alembic for migrations. Copy the URI form.
- **Transaction pooler** (port `6543`) — used by the running app for short-lived
  connections. Copy this too.

**Do not use the "Direct connection" string** (`db.<ref>.supabase.co:5432`) for
`DATABASE_URL`. It's IPv6-only unless you've bought Supabase's IPv4 add-on, and
most local networks and CI runners can't route to it — you'll hit
`psycopg.OperationalError: failed to resolve host`. The **session pooler** is
IPv4 and behaves like a direct connection (no statement-level multiplexing), so
it's safe for DDL and is what Alembic should use instead.

SQLAlchemy 2 / psycopg 3 need the driver marker in the scheme. Change the prefix
from `postgresql://` to **`postgresql+psycopg://`** in both.

## 3. Collect the API keys

**Project Settings → API.**

- Project URL → `NEXT_PUBLIC_SUPABASE_URL`
- `anon` public key → `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (server-side only — never ship
  it to the browser)

## 4. Fill in `.env`

```bash
cd session-brief
cp .env.example .env
# edit .env with the values from steps 2–3
```

- `DATABASE_URL` → the **session pooler** (`5432`) string, with `+psycopg`.
- `DATABASE_POOL_URL` → the **transaction pooler** (`6543`) string, with `+psycopg`.
- `RENDER_SHARED_SECRET` → any random string (`openssl rand -hex 32`).

## 5. Run the migration and confirm the connection

```bash
cd apps/worker
uv run alembic upgrade head        # applies 0001_initial against Supabase
uv run alembic current             # prints 0001_initial (head)
```

`alembic current` reads the live `alembic_version` table, so a correct output
proves the database is genuinely connected. That's the last item in M0's
definition of done — tick the box in `docs/08-milestones.md` once it passes.

## Later (not needed for M0)

- **RLS** is the tenancy mechanism (D10). Enable RLS on every table with
  `user_id` and add policies as those tables land in M1+.
- **Deploy env vars:** the same values go into Vercel (web) and Fly.io (worker)
  when you deploy — not into git.
