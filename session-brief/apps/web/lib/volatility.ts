// Reads for /volatility (M23). Everything here is a read of what the worker
// stored (metrics, option_strip, option_snapshot, index_daily, events); the one
// thing recomputed is the regime, which the worker deliberately never stores.
import { db } from "@/lib/db";
import {
  events,
  barsDaily,
  holdings,
  indexDaily,
  metrics,
  optionContracts,
  optionSnapshot,
  optionStrip,
  rawPayloads,
} from "@/lib/db/schema";
import { and, asc, eq, gte, inArray, lte, max, sql } from "drizzle-orm";

// Mirrors the `# --- Premium (M22) ---` block in apps/worker/worker/constants.py.
// Change both together.
export const PREMIUM = {
  richPctile: 70, // PREMIUM_RICH_PCTILE
  cheapPctile: 30, // PREMIUM_CHEAP_PCTILE
  iv30MinContracts: 4, // IV30_MIN_CONTRACTS
  earningsWindowMinDays: 30, // EARNINGS_WINDOW_MIN_DAYS
  iv30TargetDays: 30, // IV30_TARGET_DAYS
  iv30NearMinDte: 7, // IV30_NEAR_MIN_DTE
  iv30StrikesEachSide: 2, // IV30_STRIKES_EACH_SIDE
  parityTolPoints: 3, // IV30_PARITY_TOL_POINTS
  parityTolFraction: 0.1, // IV30_PARITY_TOL_FRACTION
} as const;

export const TENORS = [7, 14, 21, 30] as const;
export const STRIKE_RULES = ["itm10", "atm", "otm10"] as const;
const HISTORY_DAYS = 365;

export type Regime = "rich" | "fair" | "cheap";

/** Port of `worker.volatility.regime`, branch for branch. */
export function regime(args: {
  iv30Pctile: number | null;
  ivHvSpread: number | null;
  iv30Thin: boolean;
  ivVixRatio: number | null;
  bookMedianRatio: number | null;
}): Regime {
  const { iv30Pctile, ivHvSpread, iv30Thin, ivVixRatio, bookMedianRatio } = args;
  if (iv30Thin || ivHvSpread === null) return "fair";
  if (ivHvSpread > 0) {
    if (iv30Pctile !== null) {
      if (iv30Pctile >= PREMIUM.richPctile) return "rich";
    } else if (ivVixRatio !== null && bookMedianRatio !== null && ivVixRatio >= bookMedianRatio) {
      return "rich";
    }
  }
  if (ivHvSpread < 0 || (iv30Pctile !== null && iv30Pctile <= PREMIUM.cheapPctile)) {
    return "cheap";
  }
  return "fair";
}

/** `statistics.median`: the mean of the two middle values for an even count. */
export function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 === 1 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const METRIC_KEYS = [
  "iv30",
  "iv30_n",
  "iv30_cboe",
  "iv30_pctile",
  "iv_rank_252",
  "hv20",
  "hv60",
  "iv_hv_spread",
  "iv_hv_ratio",
  "iv_vix_ratio",
  "iv_vix_ratio_pctile",
  "vix_pctile",
  "iv_term_slope",
  "contracts_writable",
] as const;
type MetricKey = (typeof METRIC_KEYS)[number];

export type MarketLine = {
  session: string;
  vix: number | null;
  vixPctile: number | null;
  vix9d: number | null;
  vix3m: number | null;
};

export type BookRow = {
  symbol: string;
  watching: boolean;
  session: string; // the session this row's reading belongs to
  close: number | null;
  iv30: number | null;
  iv30Cboe: number | null;
  iv30Pctile: number | null;
  ivRank252: number | null;
  hv20: number | null;
  ivHvSpread: number | null;
  ivVixRatio: number | null;
  ivTermSlope: number | null;
  regime: Regime;
  earningsDate: string | null;
  contractsWritable: number | null;
  thin: boolean;
  stale: boolean;
};

export type StripCellRow = {
  tenor: number;
  rule: string;
  contract: string | null;
  expiry: string | null;
  strike: number | null;
  dte: number | null;
  moneyness: number | null;
  mid: number | null;
  premiumPct: number | null;
  timeValuePct: number | null;
  iv: number | null;
  delta: number | null;
  openInterest: number | null;
  chgContractPct: number | null;
  chgTenorPct: number | null;
  tenorCollapsed: boolean;
  targetMiss: boolean;
};

export type HistoryPoint = {
  date: string;
  iv30: number | null;
  hv20: number | null;
  earnings: boolean;
};

export type Contributor = {
  contract: string;
  expiry: string;
  right: string;
  strike: number;
  iv: number;
};

export type ExpiryIv = { expiry: string; dte: number; iv: number; n: number };

export type MethodFooter = {
  session: string | null;
  chainSession: string | null; // the snapshots read; earlier than session when stale
  spot: number | null;
  iv30: number | null; // stored, ours
  iv30Cboe: number | null; // stored, Cboe's
  iv30N: number | null; // stored
  near: ExpiryIv | null;
  far: ExpiryIv | null;
  contributors: Contributor[];
  dropped: string[]; // "YYYY-MM-DD strike" pairs the parity check removed
  capturedAt: string | null;
};

// --- helpers -----------------------------------------------------------------

function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function days(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}

function addDays(d: string, n: number): string {
  return new Date(Date.parse(`${d}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
}

async function heldSymbols(userId: string) {
  return db
    .select({ symbol: holdings.symbol, status: holdings.status })
    .from(holdings)
    .where(eq(holdings.userId, userId))
    .orderBy(asc(holdings.symbol));
}

/** Latest `option_strip` session per symbol, at or before `upTo` when given. */
async function stripSessions(symbols: string[], upTo?: string): Promise<Map<string, string>> {
  if (symbols.length === 0) return new Map();
  const rows = await db
    .select({ symbol: optionStrip.symbol, session: max(optionStrip.sessionDate) })
    .from(optionStrip)
    .where(
      and(
        inArray(optionStrip.symbol, symbols),
        upTo ? lte(optionStrip.sessionDate, upTo) : undefined,
      ),
    )
    .groupBy(optionStrip.symbol);
  const out = new Map<string, string>();
  for (const r of rows) if (r.session) out.set(r.symbol, r.session);
  return out;
}

/** Latest `option_snapshot` session per underlying, at or before `upTo`. */
async function snapshotSessions(symbols: string[], upTo: string): Promise<Map<string, string>> {
  if (symbols.length === 0) return new Map();
  const rows = await db
    .select({ symbol: optionContracts.symbol, session: max(optionSnapshot.sessionDate) })
    .from(optionSnapshot)
    .innerJoin(optionContracts, eq(optionContracts.contract, optionSnapshot.contract))
    .where(and(inArray(optionContracts.symbol, symbols), lte(optionSnapshot.sessionDate, upTo)))
    .groupBy(optionContracts.symbol);
  const out = new Map<string, string>();
  for (const r of rows) if (r.session) out.set(r.symbol, r.session);
  return out;
}

type ChainRow = {
  contract: string;
  expiry: string;
  right: string;
  strike: number;
  bid: number;
  ask: number;
  iv: number | null;
  capturedAt: string;
};

async function readChain(symbol: string, session: string): Promise<ChainRow[]> {
  const rows = await db
    .select({
      contract: optionSnapshot.contract,
      expiry: optionContracts.expirationDate,
      right: optionContracts.putOrCall,
      strike: optionContracts.strike,
      bid: optionSnapshot.bid,
      ask: optionSnapshot.ask,
      iv: optionSnapshot.iv,
      capturedAt: optionSnapshot.capturedAt,
    })
    .from(optionSnapshot)
    .innerJoin(optionContracts, eq(optionContracts.contract, optionSnapshot.contract))
    .where(and(eq(optionContracts.symbol, symbol), eq(optionSnapshot.sessionDate, session)))
    .orderBy(asc(optionSnapshot.contract));
  return rows.map((r) => ({
    contract: r.contract,
    expiry: r.expiry,
    right: r.right,
    strike: Number(r.strike),
    bid: Number(r.bid),
    ask: Number(r.ask),
    iv: num(r.iv),
    capturedAt: r.capturedAt,
  }));
}

/** The close the worker measured the chain against: the stored chain payload's
 * `close`, `bars_daily.c` only when that payload is absent. */
async function spotFor(symbol: string, session: string): Promise<number | null> {
  const [payload] = await db
    .select({ close: sql<string | null>`${rawPayloads.body}->'data'->>'close'` })
    .from(rawPayloads)
    .where(
      and(
        eq(rawPayloads.source, "cboe"),
        eq(rawPayloads.endpoint, "delayed_quotes/options"),
        eq(rawPayloads.symbol, symbol),
        eq(rawPayloads.asOf, session),
      ),
    );
  const fromPayload = num(payload?.close);
  if (fromPayload !== null) return fromPayload;
  const [bar] = await db
    .select({ c: barsDaily.c })
    .from(barsDaily)
    .where(and(eq(barsDaily.symbol, symbol), eq(barsDaily.sessionDate, session)));
  return num(bar?.c);
}

/** Port of `worker.volatility.iv30_from_chain`, kept for the method footer:
 * which contracts fed IV30 and which strikes the parity check dropped. */
function iv30Breakdown(chain: ChainRow[], spot: number, session: string) {
  const eligible = chain.filter(
    (c) =>
      c.bid > 0 &&
      c.ask > c.bid &&
      c.iv !== null &&
      days(session, c.expiry) >= PREMIUM.iv30NearMinDte,
  );
  const expiries = [...new Set(eligible.map((c) => c.expiry))].sort();
  const atOrBelow = expiries.filter((e) => days(session, e) <= PREMIUM.iv30TargetDays);
  const above = expiries.filter((e) => days(session, e) > PREMIUM.iv30TargetDays);
  const nearExpiry = atOrBelow.length ? atOrBelow[atOrBelow.length - 1] : null;
  const farExpiry = above.length ? above[0] : null;
  const dropped: string[] = [];
  const contributors: Contributor[] = [];

  const expiryIv = (e: string): ExpiryIv | null => {
    const atExpiry = eligible.filter((c) => c.expiry === e);
    const strikes = [...new Set(atExpiry.map((c) => c.strike))].sort((a, b) => a - b);
    const below = strikes.filter((k) => k <= spot).slice(-PREMIUM.iv30StrikesEachSide);
    const over = strikes.filter((k) => k > spot).slice(0, PREMIUM.iv30StrikesEachSide);
    const ivs: number[] = [];
    for (const k of [...below, ...over]) {
      const call = atExpiry.find((c) => c.strike === k && c.right === "C");
      const put = atExpiry.find((c) => c.strike === k && c.right === "P");
      if (call?.iv != null && put?.iv != null) {
        const tolerance = Math.max(
          PREMIUM.parityTolPoints,
          (PREMIUM.parityTolFraction * (call.iv + put.iv)) / 2,
        );
        if (Math.abs(call.iv - put.iv) > tolerance) {
          dropped.push(`${e} ${k}`);
          continue;
        }
      }
      for (const c of [call, put]) {
        if (c?.iv != null) {
          ivs.push(c.iv);
          contributors.push({
            contract: c.contract,
            expiry: c.expiry,
            right: c.right,
            strike: c.strike,
            iv: c.iv,
          });
        }
      }
    }
    if (ivs.length === 0) return null;
    return {
      expiry: e,
      dte: days(session, e),
      iv: ivs.reduce((a, b) => a + b, 0) / ivs.length,
      n: ivs.length,
    };
  };

  const near = nearExpiry ? expiryIv(nearExpiry) : null;
  const far = farExpiry ? expiryIv(farExpiry) : null;
  return { near, far, dropped, contributors };
}

/** The worker's earnings guard: the first `earnings` event from the session to
 * the later of the IV30 far expiry and session + 30 days. */
async function earningsInWindow(
  symbol: string,
  session: string,
  farExpiry: string | null,
): Promise<string | null> {
  const minHorizon = addDays(session, PREMIUM.earningsWindowMinDays);
  const horizon = farExpiry && farExpiry > minHorizon ? farExpiry : minHorizon;
  const [row] = await db
    .select({ first: sql<string | null>`min(${events.occursAt})` })
    .from(events)
    .where(
      and(
        eq(events.symbol, symbol),
        eq(events.eventType, "earnings"),
        gte(events.occursAt, session),
        lte(events.occursAt, horizon),
      ),
    );
  return row?.first ?? null;
}

async function indexCloses(session: string): Promise<Map<string, number>> {
  const rows = await db
    .select({ symbol: indexDaily.symbol, c: indexDaily.c })
    .from(indexDaily)
    .where(
      and(
        inArray(indexDaily.symbol, ["VIX", "VIX9D", "VIX3M"]),
        eq(indexDaily.sessionDate, session),
      ),
    );
  return new Map(rows.map((r) => [r.symbol, Number(r.c)]));
}

// --- the page's two reads ----------------------------------------------------

export async function loadVolatility(
  userId: string,
): Promise<{ session: string | null; market: MarketLine | null; rows: BookRow[] }> {
  const held = await heldSymbols(userId);
  const symbols = held.map((h) => h.symbol);
  const perName = await stripSessions(symbols);
  const session = [...perName.values()].sort().at(-1) ?? null;
  if (session === null) return { session: null, market: null, rows: [] };

  const sessions = [...new Set(perName.values())];
  const metricRows = await db
    .select({
      symbol: metrics.symbol,
      sessionDate: metrics.sessionDate,
      metric: metrics.metric,
      value: metrics.value,
    })
    .from(metrics)
    .where(
      and(
        eq(metrics.userId, userId),
        inArray(metrics.symbol, [...perName.keys()]),
        inArray(metrics.sessionDate, sessions),
        inArray(metrics.metric, [...METRIC_KEYS]),
      ),
    );
  const byName = new Map<string, Partial<Record<MetricKey, number>>>();
  for (const r of metricRows) {
    if (perName.get(r.symbol) !== r.sessionDate) continue;
    const m = byName.get(r.symbol) ?? {};
    m[r.metric as MetricKey] = Number(r.value);
    byName.set(r.symbol, m);
  }
  const snapshots = await snapshotSessions([...perName.keys()], session);

  const rows: BookRow[] = [];
  for (const h of held) {
    const nameSession = perName.get(h.symbol);
    if (!nameSession) continue;
    const m = byName.get(h.symbol) ?? {};
    const chainSession = snapshots.get(h.symbol) ?? null;
    let close: number | null = null;
    let farExpiry: string | null = null;
    if (chainSession) {
      close = await spotFor(h.symbol, chainSession);
      if (close !== null) {
        const chain = await readChain(h.symbol, chainSession);
        farExpiry = iv30Breakdown(chain, close, chainSession).far?.expiry ?? null;
      }
    }
    rows.push({
      symbol: h.symbol,
      watching: h.status === "watching",
      session: nameSession,
      close,
      iv30: m.iv30 ?? null,
      iv30Cboe: m.iv30_cboe ?? null,
      iv30Pctile: m.iv30_pctile ?? null,
      ivRank252: m.iv_rank_252 ?? null,
      hv20: m.hv20 ?? null,
      ivHvSpread: m.iv_hv_spread ?? null,
      ivVixRatio: m.iv_vix_ratio ?? null,
      ivTermSlope: m.iv_term_slope ?? null,
      regime: "fair", // set below, once the book's median ratio is known
      earningsDate: await earningsInWindow(h.symbol, nameSession, farExpiry),
      contractsWritable: m.contracts_writable ?? null,
      thin: (m.iv30_n ?? 0) < PREMIUM.iv30MinContracts,
      stale: nameSession < session || chainSession === null || chainSession < nameSession,
    });
  }

  // The fallback compares against the median IV/VIX ratio of the session's rows.
  const bookMedianRatio = median(
    rows
      .filter((r) => r.session === session && r.ivVixRatio !== null)
      .map((r) => r.ivVixRatio ?? 0),
  );
  for (const r of rows) {
    r.regime = regime({
      iv30Pctile: r.iv30Pctile,
      ivHvSpread: r.ivHvSpread,
      iv30Thin: r.thin,
      ivVixRatio: r.ivVixRatio,
      bookMedianRatio,
    });
  }
  rows.sort((a, b) => {
    if (a.iv30Pctile === null && b.iv30Pctile === null) return a.symbol.localeCompare(b.symbol);
    if (a.iv30Pctile === null) return 1;
    if (b.iv30Pctile === null) return -1;
    return b.iv30Pctile - a.iv30Pctile || a.symbol.localeCompare(b.symbol);
  });

  const closes = await indexCloses(session);
  // vix_pctile is the same on every row of a session; read it off any of them.
  const vixPctile =
    rows
      .filter((r) => r.session === session)
      .map((r) => byName.get(r.symbol)?.vix_pctile)
      .find((v) => v !== undefined) ?? null;
  const market: MarketLine = {
    session,
    vix: closes.get("VIX") ?? null,
    vixPctile,
    vix9d: closes.get("VIX9D") ?? null,
    vix3m: closes.get("VIX3M") ?? null,
  };
  return { session, market, rows };
}

export async function loadName(
  userId: string,
  symbol: string,
): Promise<{ strip: StripCellRow[]; history: HistoryPoint[]; method: MethodFooter }> {
  const session = (await stripSessions([symbol])).get(symbol) ?? null;
  const empty: MethodFooter = {
    session,
    chainSession: null,
    spot: null,
    iv30: null,
    iv30Cboe: null,
    iv30N: null,
    near: null,
    far: null,
    contributors: [],
    dropped: [],
    capturedAt: null,
  };
  if (session === null) return { strip: [], history: [], method: empty };

  const stripRows = await db
    .select({
      tenor: optionStrip.tenor,
      rule: optionStrip.strikeRule,
      contract: optionStrip.contract,
      expiry: optionContracts.expirationDate,
      strike: optionContracts.strike,
      dte: optionStrip.dte,
      moneyness: optionStrip.moneyness,
      mid: optionStrip.mid,
      premiumPct: optionStrip.premiumPct,
      timeValuePct: optionStrip.timeValuePct,
      iv: optionStrip.iv,
      delta: optionStrip.delta,
      openInterest: optionStrip.openInterest,
      chgContractPct: optionStrip.chgContractPct,
      chgTenorPct: optionStrip.chgTenorPct,
      tenorCollapsed: optionStrip.tenorCollapsed,
      targetMiss: optionStrip.targetMiss,
    })
    .from(optionStrip)
    .leftJoin(optionContracts, eq(optionContracts.contract, optionStrip.contract))
    .where(and(eq(optionStrip.symbol, symbol), eq(optionStrip.sessionDate, session)))
    .orderBy(asc(optionStrip.tenor));
  const strip: StripCellRow[] = stripRows.map((r) => ({
    tenor: r.tenor,
    rule: r.rule,
    contract: r.contract,
    expiry: r.expiry,
    strike: num(r.strike),
    dte: r.dte,
    moneyness: num(r.moneyness),
    mid: num(r.mid),
    premiumPct: num(r.premiumPct),
    timeValuePct: num(r.timeValuePct),
    iv: num(r.iv),
    delta: num(r.delta),
    openInterest: r.openInterest,
    chgContractPct: num(r.chgContractPct),
    chgTenorPct: num(r.chgTenorPct),
    tenorCollapsed: r.tenorCollapsed,
    targetMiss: r.targetMiss,
  }));

  const since = addDays(session, -HISTORY_DAYS);
  const [historyRows, earningsRows] = await Promise.all([
    db
      .select({ date: metrics.sessionDate, metric: metrics.metric, value: metrics.value })
      .from(metrics)
      .where(
        and(
          eq(metrics.userId, userId),
          eq(metrics.symbol, symbol),
          inArray(metrics.metric, ["iv30", "hv20"]),
          gte(metrics.sessionDate, since),
          lte(metrics.sessionDate, session),
        ),
      )
      .orderBy(asc(metrics.sessionDate)),
    db
      .select({ occursAt: events.occursAt })
      .from(events)
      .where(
        and(
          eq(events.symbol, symbol),
          eq(events.eventType, "earnings"),
          gte(events.occursAt, since),
          lte(events.occursAt, session),
        ),
      ),
  ]);
  const points = new Map<string, HistoryPoint>();
  for (const r of historyRows) {
    const p = points.get(r.date) ?? { date: r.date, iv30: null, hv20: null, earnings: false };
    if (r.metric === "iv30") p.iv30 = Number(r.value);
    else p.hv20 = Number(r.value);
    points.set(r.date, p);
  }
  const history = [...points.values()];
  // An earnings date that is not itself a stored session marks the next point
  // after it; one before the first stored point is outside the chart.
  for (const e of earningsRows) {
    const i = history.findIndex((p) => p.date >= e.occursAt);
    if (i > 0 || (i === 0 && history[0].date === e.occursAt)) history[i].earnings = true;
  }

  const [stored, chainSession] = await Promise.all([
    db
      .select({ metric: metrics.metric, value: metrics.value })
      .from(metrics)
      .where(
        and(
          eq(metrics.userId, userId),
          eq(metrics.symbol, symbol),
          eq(metrics.sessionDate, session),
          inArray(metrics.metric, ["iv30", "iv30_cboe", "iv30_n"]),
        ),
      ),
    snapshotSessions([symbol], session).then((m) => m.get(symbol) ?? null),
  ]);
  const s = new Map(stored.map((r) => [r.metric, Number(r.value)]));
  const method: MethodFooter = {
    ...empty,
    iv30: s.get("iv30") ?? null,
    iv30Cboe: s.get("iv30_cboe") ?? null,
    iv30N: s.get("iv30_n") ?? null,
    chainSession,
  };
  if (chainSession) {
    const [chain, spot] = await Promise.all([
      readChain(symbol, chainSession),
      spotFor(symbol, chainSession),
    ]);
    method.spot = spot;
    method.capturedAt = chain.map((c) => c.capturedAt).sort()[0] ?? null;
    if (spot !== null) Object.assign(method, iv30Breakdown(chain, spot, chainSession));
  }
  return { strip, history, method };
}

// --- formatting (shared by the page and its two components) ---------------

const DASH = "—";
const MINUS = "−";

function signed(v: number, digits: number): string {
  const s = Math.abs(v).toFixed(digits);
  return v > 0 ? `+${s}` : v < 0 && Number(s) !== 0 ? `${MINUS}${s}` : s;
}

export const fmt = {
  dash: DASH,
  /** A plain number, `digits` decimals. */
  n: (v: number | null, digits = 2): string => (v === null ? DASH : v.toFixed(digits)),
  /** Vol points, one decimal. */
  vol: (v: number | null): string => (v === null ? DASH : v.toFixed(1)),
  /** Signed vol points, one decimal. */
  volChg: (v: number | null): string => (v === null ? DASH : signed(v, 1)),
  /** A fraction as a percent. */
  pct: (v: number | null, digits = 1): string =>
    v === null ? DASH : `${(v * 100).toFixed(digits)}%`,
  /** A fraction as a signed percent. */
  pctChg: (v: number | null, digits = 1): string =>
    v === null ? DASH : `${signed(v * 100, digits)}%`,
  /** A 0–100 percentile as an ordinal. */
  ordinal: (v: number | null): string => {
    if (v === null) return DASH;
    const n = Math.round(v);
    const tail = n % 100 >= 11 && n % 100 <= 13 ? "th" : (["th", "st", "nd", "rd"][n % 10] ?? "th");
    return `${n}${tail}`;
  },
  int: (v: number | null): string => (v === null ? DASH : Math.round(v).toLocaleString("en-US")),
};
