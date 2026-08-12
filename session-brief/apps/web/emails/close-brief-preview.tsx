import type { BriefObject } from "@/lib/contracts/brief";
import { CloseBrief } from "./close-brief";

// A realistic sample (the design-reference numbers) so `pnpm --filter web email`
// exercises every section — prose, flags, resolved claims — without a database.
const sample: BriefObject = {
  schema_version: 3,
  brief_id: "preview-2026-08-11-close",
  user_id: "00000000-0000-0000-0000-000000000001",
  session_date: "2026-08-11",
  kind: "close",
  generated_at: "2026-08-11T20:42:11Z",
  subject: "Close · Tue Aug 11 — book +1.1% (+$1,746), SNDK carried it",
  one_thing:
    "You made money on a red day, and it was one name. SNDK contributed 140 of your 111 bps — without it the book was down. That's concentration working for you today, the same mechanism that works against you on another day.",
  book: {
    value_cents: 15978642,
    day_pnl_cents: 174600,
    day_bps: 111,
    total_pnl_cents: 1886300,
    total_pct: 0.134,
    vs_spy_bps: 173,
  },
  sections: [
    {
      id: "attribution",
      tier: "full",
      rows: [
        {
          symbol: "SNDK",
          close: 49.71,
          day_return: 0.059,
          day_pnl_cents: 221600,
          contribution_bps: 140,
          total_pnl_cents: 440800,
          total_pct: 0.125,
          why: "Opened +4%, added to it all session, closed on the high on 3.4× volume. Accumulation, not a gap-and-fade.",
        },
        {
          symbol: "AAOI",
          close: 18.44,
          day_return: 0.024,
          day_pnl_cents: 73100,
          contribution_bps: 46,
          total_pnl_cents: 287300,
          total_pct: 0.101,
        },
        {
          symbol: "SYM",
          close: 36.02,
          day_return: 0.007,
          day_pnl_cents: 27700,
          contribution_bps: 18,
          total_pnl_cents: 508200,
          total_pct: 0.147,
        },
        {
          symbol: "RKLB",
          close: 21.06,
          day_return: -0.009,
          day_pnl_cents: -28500,
          contribution_bps: -18,
          total_pnl_cents: 181500,
          total_pct: 0.061,
        },
        {
          symbol: "ASTS",
          close: 32.94,
          day_return: -0.064,
          day_pnl_cents: -119300,
          contribution_bps: -75,
          total_pnl_cents: 468500,
          total_pct: 0.367,
          why: "Gapped down on the downgrade and never filled. Closed near the low with volume, ahead of Thursday's lockup.",
        },
      ],
    },
    {
      id: "tape_quality",
      tier: "full",
      rows: [
        {
          symbol: "SNDK",
          rvol: 3.4,
          range_position: 0.96,
          why: "Closed on the high on heavy volume — the strongest tape of the session.",
        },
        { symbol: "ASTS", rvol: 4.1, range_position: 0.11 },
        { symbol: "SYM", rvol: 0.5, range_position: 0.58 },
      ],
    },
  ],
  flags: [
    {
      type: "correlation",
      severity: "warn",
      symbol: null,
      sector_id: null,
      value: 0.81,
      text_key: "corr_20d_high",
    },
  ],
  claims: [],
  resolved_claims: [
    {
      id: "c_863",
      symbol: "SNDK",
      type: "relative_strength",
      direction: "up",
      horizon_sessions: 1,
      outcome: "correct",
    },
  ],
  suppressed: ["SERV", "MU", "TSLA"],
  data_quality: { missing: [], stale: ["ASTS.fundamentals"] },
};

export default function CloseBriefPreview() {
  return <CloseBrief brief={sample} />;
}
