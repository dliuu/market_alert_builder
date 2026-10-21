import { font, palette } from "@/emails/theme";
import { type HistoryPoint, fmt } from "@/lib/volatility";

const W = 640;
const H = 170;
const PAD = { top: 12, right: 12, bottom: 26, left: 40 };

function dayNumber(d: string): number {
  return Date.parse(`${d}T00:00:00Z`) / 86_400_000;
}

/** IV30 against HV20 over the stored trailing year, earnings dates ticked. */
export function HistoryChart({ points }: { points: HistoryPoint[] }) {
  const values = points.flatMap((p) => [p.iv30, p.hv20]).filter((v): v is number => v !== null);
  if (values.length === 0) return <p style={S.muted}>No IV30 or HV20 stored yet.</p>;

  const x0 = dayNumber(points[0].date);
  const x1 = dayNumber(points[points.length - 1].date);
  const lo = Math.floor(Math.min(...values) - 1);
  const hi = Math.ceil(Math.max(...values) + 1);
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const x = (d: string) =>
    PAD.left + (x1 === x0 ? plotW / 2 : ((dayNumber(d) - x0) / (x1 - x0)) * plotW);
  const y = (v: number) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH;

  // Runs of consecutive non-null values; a gap breaks the line.
  const runs = (key: "iv30" | "hv20") => {
    const out: HistoryPoint[][] = [];
    let run: HistoryPoint[] = [];
    for (const p of points) {
      if (p[key] === null) {
        if (run.length) out.push(run);
        run = [];
      } else run.push(p);
    }
    if (run.length) out.push(run);
    return out;
  };
  const series = [
    { key: "iv30" as const, label: "IV30", color: palette.navy, dash: undefined },
    { key: "hv20" as const, label: "HV20", color: palette.ink3, dash: "4 3" },
  ];
  const ticks = [lo, (lo + hi) / 2, hi];

  return (
    <figure style={{ margin: 0 }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label="IV30 and HV20 history"
        style={{ display: "block", maxWidth: W }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} stroke={palette.rule2} />
            <text
              x={PAD.left - 6}
              y={y(t) + 3}
              textAnchor="end"
              fontSize={9}
              fontFamily={font.mono}
              fill={palette.ink3}
            >
              {fmt.vol(t)}
            </text>
          </g>
        ))}
        {points
          .filter((p) => p.earnings)
          .map((p) => (
            <g key={`e-${p.date}`}>
              <line
                x1={x(p.date)}
                x2={x(p.date)}
                y1={PAD.top}
                y2={H - PAD.bottom}
                stroke={palette.ink2}
                strokeDasharray="1 3"
              />
              <text
                x={x(p.date)}
                y={H - PAD.bottom + 10}
                textAnchor="middle"
                fontSize={9}
                fontFamily={font.mono}
                fill={palette.ink2}
              >
                E
              </text>
            </g>
          ))}
        {series.map((s) =>
          runs(s.key).map((run) =>
            run.length === 1 ? (
              <circle
                key={`${s.key}-${run[0].date}`}
                cx={x(run[0].date)}
                cy={y(run[0][s.key] ?? 0)}
                r={2.5}
                fill={s.color}
              />
            ) : (
              <polyline
                key={`${s.key}-${run[0].date}`}
                points={run.map((p) => `${x(p.date)},${y(p[s.key] ?? 0)}`).join(" ")}
                fill="none"
                stroke={s.color}
                strokeWidth={1.6}
                strokeDasharray={s.dash}
              />
            ),
          ),
        )}
        <text x={PAD.left} y={H - 4} fontSize={9} fontFamily={font.mono} fill={palette.ink3}>
          {points[0].date}
        </text>
        <text
          x={W - PAD.right}
          y={H - 4}
          textAnchor="end"
          fontSize={9}
          fontFamily={font.mono}
          fill={palette.ink3}
        >
          {points[points.length - 1].date}
        </text>
      </svg>
      <figcaption style={S.caption}>
        {series.map((s) => (
          <span key={s.key} style={{ marginRight: 14 }}>
            <span
              style={{
                display: "inline-block",
                width: 14,
                borderTop: `2px ${s.dash ? "dashed" : "solid"} ${s.color}`,
                verticalAlign: "middle",
                marginRight: 5,
              }}
            />
            {s.label}
          </span>
        ))}
        <span>E earnings · {points.length} sessions stored, vol points</span>
      </figcaption>
    </figure>
  );
}

const S: Record<string, React.CSSProperties> = {
  muted: { color: palette.ink3, fontSize: "0.85rem", margin: "4px 0" },
  caption: { fontFamily: font.mono, fontSize: "0.68rem", color: palette.ink3, marginTop: 4 },
};
