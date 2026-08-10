# 04 — The BriefObject

Generation and rendering are separate concerns. The pipeline produces a **versioned JSON document**; everything downstream is a renderer over it.

```
raw payloads → normalized → metrics → BriefObject → { web page, email HTML, plaintext }
```

Canonical schema: `packages/contracts/brief-object.schema.json`. Generated types on both sides via `pnpm contracts:gen`.

## Why

- The web archive and the email render from the same object and can never drift.
- The accountability loop reads `claims` as records, not regex over prose.
- Old briefs re-render under new templates without re-fetching data.
- Testing is snapshot testing: freeze one session's raw payloads, assert the object.

## Shape

```jsonc
{
  "schema_version": 3,
  "brief_id": "u_01-2026-08-11-close",
  "user_id": "u_01",
  "session_date": "2026-08-11",
  "kind": "close",                         // open | close
  "generated_at": "2026-08-11T20:42:11Z",
  "subject": "Close · Tue Aug 11 — book +1.1% (+$1,746), SNDK carried it",
  "one_thing": "You made money on a red day, and it was one name…",

  "book": {
    "value_cents": 15978642,
    "day_pnl_cents": 174610,
    "day_bps": 111,
    "total_pnl_cents": 1886300,
    "total_pct": 0.134,
    "vs_spy_bps": 173
  },

  "sections": [
    {
      "id": "attribution",
      "tier": "full",                      // full | brief | suppressed
      "rows": [
        { "symbol": "SNDK", "close": 49.71, "day_return": 0.059,
          "day_pnl_cents": 221600, "contribution_bps": 140,
          "total_pnl_cents": 440800, "total_pct": 0.125,
          "why": null }                    // filled by the narration stage
      ]
    }
  ],

  "flags": [
    { "type": "concentration", "severity": "info", "symbol": null,
      "value": 0.81, "text_key": "corr_20d_high" }
  ],

  "claims": [
    { "id": "c_881", "symbol": "SNDK", "type": "relative_strength",
      "direction": "up", "horizon_sessions": 1, "outcome": null }
  ],

  "resolved_claims": [
    { "id": "c_863", "symbol": "SNDK", "type": "catalyst_pending",
      "outcome": "correct" }
  ],

  "suppressed": ["SERV", "MU", "TSLA"],
  "data_quality": { "missing": [], "stale": ["ASTS.fundamentals"] }
}
```

## Rules

- **`why` and `one_thing` are the only free-text fields the LLM writes.** Everything else is computed. See the narration contract below.
- **Money is integer cents.** Never float, anywhere in the object.
- **Bump `schema_version` on any shape change** and keep old renderers. You will want to read year-old briefs.
- **`tier` drives suppression.** The renderer never decides what to hide; assembly does.

## Narration contract

Stage ⑤ sends the *computed* object and expects JSON back, keyed by section id:

```jsonc
{
  "one_thing": "…",
  "why": { "SNDK": "…", "ASTS": "…" },
  "sector_notes": { "semis": "…" }
}
```

Three rules:

1. **The model never produces a number.** Prompt it to write causal prose and let the tables carry the figures. Any digit in its output is a hallucination surface.
2. **Give it the news headlines** — attributing a move to a cause is the one thing it does better than the pipeline.
3. **Failure is non-fatal.** Malformed JSON or a 500 means render tables-only with a one-line note.

Validate the response against a strict schema and drop any key that doesn't match a section id in the object.
