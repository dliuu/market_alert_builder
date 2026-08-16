# cn/ — the Chinese-side of Session Brief

Everything specific to the **Chinese A-share brief pair** (open 09:10 / close
15:20 Asia/Shanghai, over a CNY-native A-share book) that *can* live outside the
deployable apps lives here, so Chinese and American features stay separable.

## Layout

```
cn/
  docs/
    2026-08-15-shanghai-briefs-design.md   # the design spec (start here)
    open-questions.md                      # CN vendor/API questions + fallbacks
    milestones.md                          # CN-M1..CN-M3, definitions of done
apps/worker/worker_cn/                     # ALL CN worker code (own package)
apps/worker/tests/cn/                      # CN tests + frozen fixtures
apps/web/emails/cn/                        # CN email templates
```

## Why the code is not in this folder

Fly builds the worker image with `apps/worker` as the Docker context
(`apps/worker/fly.toml`), so files outside it cannot ship in the image; Vercel
builds `apps/web` the same way. The separation rule is therefore:

- **CN logic lives only in `apps/worker/worker_cn/` and `apps/web/emails/cn/`**
  — dedicated packages, never mixed into `worker/` or the shared templates.
- **Shared files gain only parameters and seams** (a `market` filter on reads, a
  `currency` on money rendering, an import of the CN scheduler jobs), never CN
  business logic.
- **Schema and contract stay shared by necessity** — one database, one Alembic
  chain (invariant 1), one versioned BriefObject (D1). CN briefs are two new
  `kind` values on the same object, not a parallel system.

## Status

Built through CN-M3: the CN close brief (CN-M1, PR #21), the CN open brief +
four-kind scheduler (CN-M2, PR #23), and the live Tiingo A-share swap behind
the probe (CN-M3, PR #24). See `docs/milestones.md` for each milestone's
definition of done and outstanding items (CN-M2's live soak, CN-M3's latency
half); the American-side decision log records the structural choices as D32
in `session-brief/docs/07-decisions.md`.
