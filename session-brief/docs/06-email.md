# 06 — Email delivery

Everything upstream of this works without sending a single email — briefs generate and you read them at `/briefs/2026-08-11-close`. **Run it that way for two weeks first.** The content will change substantially and iterating in a browser tab is far faster than iterating through your own inbox.

## DNS

- Send from a **subdomain** (`brief.yourdomain.com`) so the brief's reputation is isolated from personal mail.
- **SPF** TXT, **DKIM** CNAMEs from the provider, **DMARC** TXT starting at `p=none`, tightening to `p=quarantine`.
- `List-Unsubscribe` + `List-Unsubscribe-Post` headers. Not required at two-emails-a-day volume; ten minutes now versus a debugging session at scale.

## Provider

**Resend** — 3,000/month free, best-in-class DX, and React Email is a first-party path. Covers ~40 sends/day indefinitely, which is roughly 60 users on twice-daily briefs before you pay.

**Postmark** (~$15/mo) if inbox placement ever matters more than cost; measured placement is meaningfully better than raw SES. **SES** at $0.10/1,000 only wins past ~200k/month, which this will never reach.

## Email HTML is not web HTML

This is the real work, and it changes the design reference:

- **Tables for layout.** No flexbox, no grid — Outlook on Windows renders through Word.
- **Inline every style.** `<style>` blocks are stripped by several clients.
- **600px fixed width.** Fluid layouts break in more places than they help.
- **Web fonts don't load in Outlook.** Archivo / Newsreader / IBM Plex Mono will fall back — set the fallback stacks deliberately and check the fallback looks intentional rather than accidental.
- **The range-position bars** become nested table cells with `bgcolor`, not absolutely-positioned divs.
- **The highlighter gradient** on "the one thing" needs a solid `bgcolor` fallback on the parent cell.
- **Gmail clips at ~102KB.** The close brief is dense — stay under 80KB, which rules out inline base64 images.
- **Dark mode** inverts unpredictably in Apple Mail and Outlook. The oxblood/pine pair survives inversion better than pure red/green, which is partly why it was chosen. Test it anyway.
- Always send a **plaintext alternative part.** Missing it is a spam signal on its own.

React Email emits the table soup for you. Preview across clients before trusting it.

## Send pipeline

```
assemble → briefs row
        → deliveries row (status=pending)
        → worker POSTs /api/render/:brief_id  → { html, text }
        → Resend API → status=sent, provider_msg_id
```

`UNIQUE (brief_id, recipient)` is the guard that stops a crashed worker's retry from mailing the same brief four times.

Handle the provider's bounce/complaint webhook even with one recipient — twenty lines, and it's how you learn your DKIM broke.

## Failure policy

| Failure | Response |
|---|---|
| Partial data fetch | Send anyway with an explicit "missing: ASTS fundamentals" line. Silent gaps are worse than visible ones. |
| Narration failed | Send tables-only with a one-line note. |
| Render endpoint down | Send the plaintext part only. |
| Assemble threw | Send a minimal fallback brief: prices and P&L only. |
| Provider 5xx | Retry 3× with backoff, then alert. **Never retry past the next scheduled send** — a stale brief arriving at 11am is worse than none. |

## Compliance

Sending only to yourself is not commercial email and CAN-SPAM doesn't attach. The moment there's a second recipient it does: physical mailing address in the footer, unsubscribe honoured within 10 days. The footer in the design reference already has the slot.
