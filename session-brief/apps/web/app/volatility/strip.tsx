import { font, palette } from "@/emails/theme";
import { STRIKE_RULES, type StripCellRow, TENORS, fmt } from "@/lib/volatility";

const RULE_LABEL: Record<string, string> = {
  itm10: "ITM 10% · 0.90×",
  atm: "ATM · 1.00×",
  otm10: "OTM 10% · 1.10×",
};

/** The call strip: four tenors down, three strike rules across. */
export function StripGrid({ cells }: { cells: StripCellRow[] }) {
  if (cells.length === 0) return <p style={S.muted}>No strip stored for this session.</p>;
  const at = new Map(cells.map((c) => [`${c.tenor}:${c.rule}`, c]));
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={S.table}>
        <thead>
          <tr>
            <th style={S.th}>Tenor</th>
            {STRIKE_RULES.map((rule) => (
              <th key={rule} style={S.th}>
                {RULE_LABEL[rule]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {TENORS.map((tenor) => {
            const row = STRIKE_RULES.map((rule) => at.get(`${tenor}:${rule}`));
            const first = row.find((c) => c?.contract);
            return (
              <tr key={tenor}>
                <td style={S.tenor}>
                  <div style={S.tenorLabel}>{tenor}d</div>
                  <div style={S.sub}>
                    {first
                      ? `${first.dte ?? fmt.dash} dte · ${first.expiry ?? fmt.dash}`
                      : fmt.dash}
                  </div>
                </td>
                {row.map((cell, i) => (
                  <td key={STRIKE_RULES[i]} style={S.cell}>
                    {cell ? <Cell cell={cell} /> : <span style={S.sub}>{fmt.dash}</span>}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Cell({ cell }: { cell: StripCellRow }) {
  if (!cell.contract) return <span style={S.sub}>no two-sided call</span>;
  return (
    <div>
      <div style={S.head}>
        <span>
          K {fmt.n(cell.strike)} <span style={S.sub}>{fmt.pctChg(cell.moneyness)}</span>
        </span>
        <span>
          {cell.targetMiss && <span style={S.badge}>MISS</span>}
          {cell.tenorCollapsed && <span style={S.badge}>COLLAPSED</span>}
        </span>
      </div>
      <div style={S.line}>
        mid {fmt.n(cell.mid)} · prem {fmt.pct(cell.premiumPct, 2)} · tv{" "}
        {fmt.pct(cell.timeValuePct, 2)}
      </div>
      <div style={S.line}>
        iv {fmt.vol(cell.iv)} · Δ {fmt.n(cell.delta)} · OI {fmt.int(cell.openInterest)}
      </div>
      <div style={S.sub}>
        chg contract {fmt.pctChg(cell.chgContractPct)} · chg tenor {fmt.pctChg(cell.chgTenorPct)}
      </div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  muted: { color: palette.ink3, fontSize: "0.85rem", margin: "4px 0" },
  table: { borderCollapse: "collapse", width: "100%", fontFamily: font.mono, fontSize: "0.74rem" },
  th: {
    textAlign: "left",
    padding: "4px 10px 6px 0",
    color: palette.ink3,
    fontWeight: 500,
    fontSize: "0.66rem",
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    borderBottom: `1px solid ${palette.rule}`,
  },
  tenor: {
    verticalAlign: "top",
    padding: "8px 12px 8px 0",
    borderBottom: `1px solid ${palette.rule2}`,
    whiteSpace: "nowrap",
  },
  tenorLabel: { fontWeight: 600, color: palette.ink, fontSize: "0.85rem" },
  cell: {
    verticalAlign: "top",
    padding: "8px 12px 8px 0",
    borderBottom: `1px solid ${palette.rule2}`,
    minWidth: 190,
  },
  head: {
    display: "flex",
    justifyContent: "space-between",
    gap: 6,
    fontWeight: 600,
    color: palette.ink,
  },
  line: { color: palette.ink, marginTop: 2 },
  sub: { color: palette.ink3, marginTop: 2 },
  badge: {
    marginLeft: 4,
    fontSize: "0.6rem",
    letterSpacing: "0.06em",
    color: palette.ink2,
    border: `1px solid ${palette.rule}`,
    padding: "0 4px",
    borderRadius: 2,
  },
};
