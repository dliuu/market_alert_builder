# M10 — Scheduler + dead-man's switch

**Milestone (docs/08):** *Done when:* it runs five consecutive sessions
untouched, including one market holiday it correctly skips.

## Scope decisions

- **Close pipeline only.** The scheduler drives the close brief end to end
  (poll → compute → assemble → narrate → send). The open brief's 08:00/08:10/
  08:15 staging (docs/02) isn't scheduled — no pre-market pipeline exists to run
  (same precedent as M6/M7/M9). The seam is a second fire time when it lands.
- **Real Fly.io deploy.** "Five sessions untouched" is only honest on an
  unattended machine, so M10 ships `Dockerfile` + `fly.toml`, not just a fast-
  forward test.
- **EOD-lag → bounded poll (user decision).** Tiingo may publish minutes after
  the bell; the run polls for today's bar rather than sending stale or failing on
  the first miss.

## Design

Mirrors `claims.py`/`flags.py`: a **pure core** + a thin **orchestration layer**,
kept out of the M3-certified compute.

- **`worker/calendar.py`** — the invariant-7 wrapper over `exchange_calendars`
  (`XNYS`, now a worker dep). `is_session`, `session_close` (real close; 13:00 ET
  on half-days, DST handled), `next_session`, `today_et` (UTC→ET session date),
  `close_or_standard` (real close on a session, nominal 16:00 ET bell otherwise).
- **`worker/scheduler.py`**
  - Pure: `fire_time(d, delay)` = `close_or_standard(d) + delay`;
    `next_fire(now, delay)` walks forward to the next fire strictly after `now`.
  - `run_session_job(now_utc, …)` — one day's go/no-go. `is_session` false →
    ping success, return `skipped-holiday`. Session → `ensure_todays_bars`
    (poll) → `assemble_and_store` (own txn, committed) → `None` means quiet skip
    (ping success, `skipped-quiet`) → `deliver_brief` (own txn) → ping success,
    `sent`. Any exception → ping `<url>/fail`, re-raise.
  - `ensure_todays_bars` — idempotent ingest+normalize in a loop until every
    symbol has today's bar or `BAR_POLL_TIMEOUT_S` elapses; returns what's still
    missing (compute then raises → `/fail`, never a stale send — docs/06).
  - `book_symbols` — held names ∪ `SPY` (the benchmark the vs-SPY line always
    needs; the manual `backfill` omitted it).
  - `run_scheduler` — `BlockingScheduler` + a single self-rescheduling one-shot
    `DateTrigger`. Fires **every calendar day**; sends only on sessions; pings
    every run. Loop survives a bad day (logs, reschedules tomorrow).
  - Ping helpers swallow network errors — a monitoring outage must not stop a
    send.
- **CLI** — `schedule` (blocking loop), `--once` (**live** single run; it sends),
  `--dry-run` (prints next fire times, touches nothing — the safe smoke check).
- **Config / `.env`** — `HEALTHCHECKS_URL`, `SEND_DELAY_MINUTES` (45),
  `BAR_POLL_TIMEOUT_S` (1200), `BAR_POLL_INTERVAL_S` (90).
- **Deploy** — `apps/worker/{Dockerfile,fly.toml,.dockerignore}`: one always-on
  machine, no HTTP service (no cold starts), `alembic upgrade head` as the Fly
  `release_command` (invariant 1). No new migration — M10 adds no tables.

## Why fire every calendar day (not a weekday cron)

Healthchecks' cron can encode weekdays but **not NYSE holidays**. A weekday-only
schedule would read Labor Day as a *missed* check-in and go red — the opposite of
"correctly skips." Firing daily and pinging success on every run (send, quiet
skip, or holiday skip) makes the holiday an explicit green heartbeat; only a
crash or a dead process turns the check red.

## Why a self-rescheduling one-shot, not a fixed cron

The send must track the close: 16:45 ET normally, 13:45 ET on a half-day. A cron
at a fixed wall-clock time can't. Computing the next fire off the *real* close
each run makes half-days move the send with zero special-casing (docs/02).

## Testing

- **Pure (`test_calendar.py`):** session/weekend/holiday classification, regular
  vs half-day close, `next_session` skipping the holiday, `today_et` across the
  UTC-midnight boundary — checked against real NYSE 2026 dates (Labor Day
  2026-09-07; the Thanksgiving half-day 2026-11-27).
- **Pure (`test_scheduler.py`):** `fire_time` (regular + half-day), `next_fire`
  rolling past a just-passed time and landing on the holiday as a heartbeat; the
  poll loop (waits then succeeds; gives up at timeout); the ping helpers
  (correct URLs, empty-URL no-op, network errors swallowed); `run_session_job`
  holiday-skip (success ping, no `/fail`) and failure (`/fail` then re-raise).
- The DoD proper — five sessions untouched with a holiday skip — is a **live
  soak** on Fly, not a unit test. See the manual test plan.

## Out of scope (deferred)

The open-brief pipeline and its fire time; swapping `flags.py`'s calendar-day
earnings/supply proximity for true session-counting now that the calendar exists
(D18's noted one-line change — the seam stays); a leader-lock for running more
than one worker machine.
