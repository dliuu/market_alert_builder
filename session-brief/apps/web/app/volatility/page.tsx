import { font, palette } from "@/emails/theme";
import { DEV_USER_ID } from "@/lib/constants";
import {
  type BookRow,
  type MarketLine,
  type MethodFooter,
  type Regime,
  fmt,
  loadName,
  loadVolatility,
} from "@/lib/volatility";
import { HistoryChart } from "./history";
import { StripGrid } from "./strip";

export const dynamic = "force-dynamic";

export default async function VolatilityPage() {
  const { session, market, rows } = await loadVolatility(DEV_USER_ID);
  const names = await Promise.all(rows.map((r) => loadName(DEV_USER_ID, r.symbol)));

  return (
    <main style={S.main}>
      <p style={S.crumb}>
        <a href="/" style={S.link}>
          ← Session Brief
        </a>
      </p>
      <p style={S.eyebrow}>Premium and volatility</p>
      <h1 style={S.h1}>Option premium across the book</h1>

      {session === null || market === null ? (
        <p style={S.note}>
          No volatility rows yet. The close job stores them after the options pull (
          <code>uv run -m worker.cli options pull</code>).
        </p>
      ) : (
        <>
          <Market market={market} />
          <BookTable rows={rows} />
          {rows.map((r, i) => (
            <NameDetail key={r.symbol} row={r} {...names[i]} />
          ))}
        </>
      )}

      <footer style={S.foot}>
        A measurement of premium against its own history, realized volatility and the VIX; not
        advice.
      </footer>
    </main>
  );
}

function Market({ market }: { market: MarketLine }) {
  const stats: [string, string, string?][] = [
    ["Session", market.session],
    [
      "VIX",
      fmt.n(market.vix),
      market.vixPctile !== null ? `${fmt.ordinal(market.vixPctile)} pctile` : "pctile —",
    ],
    ["VIX9D", fmt.n(market.vix9d)],
    ["VIX3M", fmt.n(market.vix3m)],
  ];
  return (
    <section style={S.card}>
      <div style={S.market}>
        {stats.map(([k, v, sub]) => (
          <div key={k}>
            <div style={S.k}>{k}</div>
            <div style={S.v}>
              {v} {sub && <span style={S.vSub}>{sub}</span>}
            </div>
          </div>
        ))}
      </div>
      <p style={S.attribution}>
        Option chains and VIX, VIX9D, VIX3M closes: Cboe delayed market data, cboe.com.
      </p>
    </section>
  );
}

const COLUMNS: [string, (r: BookRow) => React.ReactNode][] = [
  ["Close", (r) => fmt.n(r.close)],
  ["IV30", (r) => fmt.vol(r.iv30)],
  ["Cboe IV30", (r) => fmt.vol(r.iv30Cboe)],
  ["IV30 pctile", (r) => fmt.ordinal(r.iv30Pctile)],
  ["IV rank", (r) => (r.ivRank252 === null ? fmt.dash : fmt.n(r.ivRank252 * 100, 0))],
  ["HV20", (r) => fmt.vol(r.hv20)],
  ["IV−HV", (r) => fmt.volChg(r.ivHvSpread)],
  ["IV/VIX", (r) => fmt.n(r.ivVixRatio)],
  ["Term slope", (r) => fmt.volChg(r.ivTermSlope)],
  ["Regime", (r) => <RegimeWord regime={r.regime} />],
  ["Earnings", (r) => r.earningsDate ?? fmt.dash],
  ["Contracts writable", (r) => fmt.int(r.contractsWritable)],
  ["Thin", (r) => (r.thin ? "thin" : "")],
  ["Stale", (r) => (r.stale ? "stale" : "")],
];

function BookTable({ rows }: { rows: BookRow[] }) {
  return (
    <section style={{ margin: "22px 0" }}>
      <p style={S.eyebrow}>The book · sorted by IV30 percentile</p>
      <div
        style={{
          overflowX: "auto",
          background: palette.white,
          border: `1px solid ${palette.rule}`,
        }}
      >
        <table style={S.table}>
          <thead>
            <tr>
              <th style={S.th}>Name</th>
              {COLUMNS.map(([label]) => (
                <th key={label} style={S.thR}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol}>
                <td style={S.td}>
                  <a href={`#${r.symbol}`} style={S.link}>
                    <strong>{r.symbol}</strong>
                  </a>
                  {r.watching && <span style={S.tag}>watching</span>}
                </td>
                {COLUMNS.map(([label, cell]) => (
                  <td key={label} style={S.tdR}>
                    {cell(r)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={S.note}>
        IV30 and HV20 in vol points. Percentiles rank against the name's own prior 252 sessions and
        stay blank until a full year is stored. Rich: implied over realized and high for its year;
        cheap: implied under realized or low for its year; fair otherwise, and always when thin
        (fewer than four contracts behind IV30).
      </p>
    </section>
  );
}

function RegimeWord({ regime }: { regime: Regime }) {
  return (
    <span
      style={{
        fontStyle: "italic",
        fontFamily: font.body,
        color: regime === "fair" ? palette.ink3 : palette.ink2,
        fontWeight: regime === "fair" ? 400 : 600,
      }}
    >
      {regime}
    </span>
  );
}

function NameDetail({
  row,
  strip,
  history,
  method,
}: {
  row: BookRow;
  strip: Awaited<ReturnType<typeof loadName>>["strip"];
  history: Awaited<ReturnType<typeof loadName>>["history"];
  method: MethodFooter;
}) {
  return (
    <details id={row.symbol} style={S.details}>
      <summary style={S.summary}>
        <strong>{row.symbol}</strong> <RegimeWord regime={row.regime} />{" "}
        <span style={S.vSub}>
          IV30 {fmt.vol(row.iv30)} · {fmt.ordinal(row.iv30Pctile)} pctile · session {row.session}
          {row.stale && " · stale"}
        </span>
      </summary>
      <div style={{ padding: "6px 0 4px" }}>
        <p style={S.eyebrow}>Call strip · tenor by strike</p>
        <StripGrid cells={strip} />
        <p style={{ ...S.note, marginTop: 6 }}>
          prem = mid / close; tv = time value / close; chg contract = the same contract's mid vs the
          prior session; chg tenor = the same cell's prem vs the prior session. MISS: the nearest
          listed strike is more than 2.5% from the target. COLLAPSED: a shorter tenor already took
          this expiry.
        </p>
        <p style={{ ...S.eyebrow, marginTop: 18 }}>IV30 against HV20 · trailing year</p>
        <HistoryChart points={history} />
        <Method method={method} />
      </div>
    </details>
  );
}

function Method({ method }: { method: MethodFooter }) {
  const legs = [method.near, method.far].filter((l) => l !== null);
  return (
    <div style={S.method}>
      <div>
        IV30 ours {fmt.vol(method.iv30)} · Cboe {fmt.vol(method.iv30Cboe)} · {fmt.int(method.iv30N)}{" "}
        contracts · spot {fmt.n(method.spot)} · chain {method.chainSession ?? fmt.dash} · captured{" "}
        {method.capturedAt ?? fmt.dash}
      </div>
      {legs.map((l) => (
        <div key={l.expiry}>
          {l.expiry} ({l.dte} dte): mean iv {fmt.vol(l.iv)} over {l.n}
        </div>
      ))}
      {method.contributors.length > 0 && (
        <div>
          Contributors:{" "}
          {method.contributors
            .map((c) => `${c.expiry} ${fmt.n(c.strike)}${c.right} ${fmt.vol(c.iv)}`)
            .join(" · ")}
        </div>
      )}
      <div>Parity drops: {method.dropped.length > 0 ? method.dropped.join(" · ") : "none"}</div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  main: {
    fontFamily: font.disp,
    color: palette.ink,
    background: palette.paper,
    minHeight: "100vh",
    padding: "2rem",
    maxWidth: 1180,
    margin: "0 auto",
  },
  crumb: { fontSize: "0.85rem", margin: "0 0 14px" },
  link: { color: palette.navy, textDecoration: "none" },
  eyebrow: {
    fontFamily: font.mono,
    fontSize: "0.66rem",
    letterSpacing: "0.13em",
    textTransform: "uppercase",
    color: palette.ink3,
    margin: "0 0 8px",
  },
  h1: { margin: "0 0 16px", fontSize: "1.8rem", fontWeight: 700, letterSpacing: "-0.03em" },
  note: { fontFamily: font.body, fontSize: "0.85rem", color: palette.ink2, lineHeight: 1.5 },
  card: {
    background: palette.card,
    border: `1px solid ${palette.rule}`,
    padding: "13px 16px",
  },
  market: { display: "flex", gap: 28, flexWrap: "wrap" },
  k: {
    fontFamily: font.mono,
    fontSize: "0.6rem",
    letterSpacing: "0.13em",
    textTransform: "uppercase",
    color: palette.ink3,
  },
  v: { fontFamily: font.mono, fontSize: "1.15rem", fontWeight: 600, letterSpacing: "-0.02em" },
  vSub: { fontFamily: font.mono, fontSize: "0.74rem", fontWeight: 400, color: palette.ink3 },
  attribution: {
    fontFamily: font.mono,
    fontSize: "0.62rem",
    color: palette.ink3,
    margin: "10px 0 0",
  },
  table: { borderCollapse: "collapse", width: "100%", fontFamily: font.mono, fontSize: "0.78rem" },
  th: {
    textAlign: "left",
    padding: "8px 10px",
    color: palette.ink3,
    fontWeight: 500,
    fontSize: "0.62rem",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    borderBottom: `1px solid ${palette.rule}`,
    whiteSpace: "nowrap",
  },
  thR: {
    textAlign: "right",
    padding: "8px 10px",
    color: palette.ink3,
    fontWeight: 500,
    fontSize: "0.62rem",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    borderBottom: `1px solid ${palette.rule}`,
    whiteSpace: "nowrap",
  },
  td: { padding: "7px 10px", borderBottom: `1px solid ${palette.rule2}`, whiteSpace: "nowrap" },
  tdR: {
    padding: "7px 10px",
    borderBottom: `1px solid ${palette.rule2}`,
    textAlign: "right",
    whiteSpace: "nowrap",
  },
  tag: {
    marginLeft: 6,
    fontSize: "0.6rem",
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: palette.ink3,
  },
  details: {
    background: palette.card,
    border: `1px solid ${palette.rule}`,
    padding: "10px 16px",
    margin: "10px 0",
  },
  summary: { cursor: "pointer", fontSize: "0.95rem" },
  method: {
    marginTop: 16,
    borderTop: `1px solid ${palette.rule}`,
    paddingTop: 10,
    fontFamily: font.mono,
    fontSize: "0.66rem",
    lineHeight: 1.8,
    color: palette.ink3,
  },
  foot: {
    marginTop: 40,
    borderTop: `1px solid ${palette.rule}`,
    paddingTop: 14,
    fontFamily: font.mono,
    fontSize: "0.62rem",
    letterSpacing: "0.04em",
    lineHeight: 1.8,
    color: palette.ink3,
  },
};
