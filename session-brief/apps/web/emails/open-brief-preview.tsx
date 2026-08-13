import type { BriefObject } from "@/lib/contracts/brief";
import { OpenBrief } from "./open-brief";

// A realistic sample so `pnpm --filter web email` exercises every open-brief
// section — prose, a mixed calendar, sectors with and without a benchmark, and
// a fired flag — without a database.
const sample: BriefObject = {
  schema_version: 3,
  brief_id: "preview-2026-08-13-open",
  user_id: "00000000-0000-0000-0000-000000000001",
  session_date: "2026-08-13",
  kind: "open",
  generated_at: "2026-08-13T12:15:00Z",
  subject: "Open · Thu Aug 13 — the day ahead",
  one_thing:
    "Your largest position reports before the bell, and it is most of the book. Semis have been carrying the tape all week, so the print lands into a market already leaning that way — which cuts both directions.",
  // no `book`: the open brief carries no performance and no P&L
  sections: [
    {
      id: "overnight_tape",
      tier: "suppressed",
      note: "Overnight tape lands with the futures and macro feed.",
      rows: [],
    },
    {
      id: "premarket",
      tier: "suppressed",
      note: "Pre-market moves land with the delayed-quote feed.",
      rows: [],
    },
    {
      id: "calendar",
      tier: "full",
      note: null,
      rows: [
        {
          symbol: null,
          label: "CPI (m/m)",
          event_type: "macro",
          occurs_at: "2026-08-13",
          tag: "macro",
        },
        {
          symbol: "SNDK",
          label: "SNDK Q2 earnings",
          event_type: "earnings",
          occurs_at: "2026-08-13",
          tag: "holding",
        },
        {
          symbol: "ASTS",
          label: "ASTS ex-dividend",
          event_type: "ex_div",
          occurs_at: "2026-08-16",
          tag: "watchlist",
        },
        {
          symbol: null,
          label: "FOMC rate decision",
          event_type: "macro",
          occurs_at: "2026-08-19",
          tag: "macro",
        },
      ],
    },
    {
      id: "sector_setup",
      tier: "full",
      note: null,
      rows: [
        {
          sector_id: "s1",
          name: "Semis",
          benchmark_symbol: "SMH",
          ret_5d: 0.0293,
          vs_spy_5d: 0.0227,
          premarket: null,
        },
        {
          sector_id: "s2",
          name: "Space",
          benchmark_symbol: "ARKX",
          ret_5d: -0.0113,
          vs_spy_5d: -0.0179,
          premarket: null,
        },
        {
          sector_id: "s3",
          name: "Cash equivalents",
          benchmark_symbol: null,
          ret_5d: null,
          vs_spy_5d: null,
          premarket: null,
        },
      ],
    },
    { id: "exposure_check", tier: "full", note: null, rows: [] },
  ],
  flags: [
    {
      type: "concentration",
      severity: "warn",
      symbol: "SNDK",
      sector_id: null,
      value: 0.81,
      text_key: "single_name_concentration",
    },
    {
      type: "earnings_soon",
      severity: "info",
      symbol: "SNDK",
      sector_id: null,
      value: 0,
      text_key: "earnings_soon",
    },
  ],
  claims: [],
  resolved_claims: [],
  suppressed: [],
  data_quality: { missing: [], stale: [] },
};

export default function Preview() {
  return <OpenBrief brief={sample} />;
}
