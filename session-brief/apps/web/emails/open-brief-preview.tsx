import type { BriefObject } from "@/lib/contracts/brief";
import { OpenBrief } from "./open-brief";

// A realistic sample so `pnpm --filter web email` exercises every open-brief
// section — prose, a mixed calendar, sectors with and without a benchmark, and
// a fired flag — without a database.
const sample: BriefObject = {
  schema_version: 4,
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
      tier: "full",
      note: "Risk-off in US index futures, but Asian memory names ripped overnight on a Hynix pricing comment. Your semis sleeve is likely to open green against a red tape.",
      rows: [
        {
          symbol: "ES",
          label: "ES futures",
          overnight_pct: -0.0041,
          overnight_abs: null,
          level: null,
        },
        { symbol: "TNX", label: "10Y", overnight_pct: null, overnight_abs: 0.03, level: 4.28 },
        {
          symbol: "NQ",
          label: "NQ futures",
          overnight_pct: -0.0063,
          overnight_abs: null,
          level: null,
        },
        { symbol: "DXY", label: "DXY", overnight_pct: 0.0022, overnight_abs: null, level: null },
        { symbol: "VIX", label: "VIX", overnight_pct: null, overnight_abs: 1.1, level: 16.4 },
        { symbol: "WTI", label: "WTI", overnight_pct: -0.009, overnight_abs: null, level: null },
      ],
    },
    {
      id: "premarket",
      tier: "full",
      note: "Only names moving more than 1% pre-market, or carrying news, get a line. Everything else is unchanged and skipped.",
      rows: [
        {
          symbol: "SNDK",
          why: "Hynix said NAND contract pricing settles up mid-single-digits in Q4. Read-through is direct.",
          pre_pct: 0.041,
          gap_cents: 194,
          premarket_vol_mult: 3.1,
        },
        {
          symbol: "SYM",
          why: "Drifting on no news. Earnings tonight; implied move ±14%.",
          pre_pct: -0.008,
          gap_cents: -29,
          premarket_vol_mult: 0.6,
        },
        {
          symbol: "ASTS",
          why: "Downgraded to Hold at Deutsche Bank, PT cut 42 → 34. Valuation call, not a thesis break.",
          pre_pct: -0.052,
          gap_cents: -188,
          premarket_vol_mult: 4.7,
        },
      ],
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
