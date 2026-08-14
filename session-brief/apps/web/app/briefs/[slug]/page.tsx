import { DEV_USER_ID } from "@/lib/constants";
import type { BriefObject, Claim, Row } from "@/lib/contracts/brief";
import { db } from "@/lib/db";
import { briefs } from "@/lib/db/schema";
import { and, eq } from "drizzle-orm";
import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

// Slug is "<YYYY-MM-DD>-<kind>", e.g. 2026-08-11-close.
const SLUG = /^(\d{4}-\d{2}-\d{2})-(open|close)$/;

export default async function BriefPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const match = SLUG.exec(slug);
  if (!match) notFound();
  const [sessionDate, kind] = [match[1], match[2]];

  const [row] = await db
    .select()
    .from(briefs)
    .where(
      and(
        eq(briefs.userId, DEV_USER_ID),
        eq(briefs.sessionDate, sessionDate),
        eq(briefs.kind, kind),
      ),
    )
    .limit(1);

  if (!row) notFound();
  const brief = row.body as unknown as BriefObject;
  const attribution = brief.sections.find((s) => s.id === "attribution");
  const tape = brief.sections.find((s) => s.id === "tape_quality");
  const overnightTape = brief.sections.find((s) => s.id === "overnight_tape");
  // Same key as the email template: `assemble_open` appends this exact string
  // to `data_quality.stale` while `constants.PREMARKET_FEED_IS_SYNTHETIC` is
  // True. One flag, one place to flip it — nothing here changes the day a
  // licensed feed lands.
  const tapeIsSynthetic = brief.data_quality.stale.includes("overnight_tape.synthetic");
  const premarket = brief.sections.find((s) => s.id === "premarket");
  const calendar = brief.sections.find((s) => s.id === "calendar");
  const sectors = brief.sections.find((s) => s.id === "sector_setup");
  // §2/§3 render their own card (table + note) once they carry rows; a
  // suppressed section with no rows falls through to this generic note list
  // instead, so a quiet section's note prints exactly once either way.
  const omitted = brief.sections
    .filter(
      (s) => (s.id === "overnight_tape" || s.id === "premarket") && s.note && s.rows.length === 0,
    )
    .map((s) => s.note);

  return (
    <main style={S.main}>
      <p style={S.crumb}>
        <a href="/briefs">← briefs</a>
      </p>
      <h1 style={S.subject}>{brief.subject}</h1>
      <p style={S.muted}>
        {kind} · {sessionDate} · schema v{brief.schema_version}
      </p>

      {brief.one_thing && <p style={S.oneThing}>{brief.one_thing}</p>}

      {/* Scorecard — from book totals. `book` is nullable since v3: the open
          brief carries no performance or P&L at all (M14). */}
      {brief.book && (
        <section style={S.card}>
          <h2 style={S.h2}>Session scorecard</h2>
          <div style={S.grid}>
            <Stat label="Book value" value={dollars(brief.book.value_cents)} />
            <Stat
              label="Day P&L"
              value={`${signedDollars(brief.book.day_pnl_cents)} · ${bps(brief.book.day_bps)}`}
              positive={brief.book.day_pnl_cents >= 0}
            />
            <Stat
              label="Total P&L"
              value={`${signedDollars(brief.book.total_pnl_cents)}${
                brief.book.total_pct != null ? ` · ${pct(brief.book.total_pct)}` : ""
              }`}
              positive={brief.book.total_pnl_cents >= 0}
            />
            {brief.book.vs_spy_bps != null && (
              <Stat
                label="vs SPY"
                value={bps(brief.book.vs_spy_bps)}
                positive={brief.book.vs_spy_bps >= 0}
              />
            )}
          </div>
        </section>
      )}

      {/* Attribution */}
      {attribution && (
        <section style={S.card}>
          <h2 style={S.h2}>Attribution</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Symbol</th>
                <th style={S.thR}>Close</th>
                <th style={S.thR}>Day %</th>
                <th style={S.thR}>Day P&L</th>
                <th style={S.thR}>Contrib</th>
                <th style={S.thR}>Resid</th>
                <th style={S.thR}>Total P&L</th>
                <th style={S.thR}>Total %</th>
              </tr>
            </thead>
            <tbody>
              {attribution.rows.map((r: Row) => (
                <tr key={r.symbol}>
                  <td style={S.td}>
                    {r.symbol}
                    {r.provisional && (
                      <sup style={S.provMarker} title="provisional — not yet reconciled with fills">
                        p
                      </sup>
                    )}
                  </td>
                  <td style={S.tdR}>{r.close != null ? `$${r.close.toFixed(2)}` : "—"}</td>
                  <td style={{ ...S.tdR, ...signColor(r.day_return) }}>
                    {pctOrDash(r.day_return)}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.day_pnl_cents) }}>
                    {r.day_pnl_cents != null ? signedDollars(r.day_pnl_cents) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.contribution_bps) }}>
                    {r.contribution_bps != null ? bps(r.contribution_bps) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.resid_bps) }}>
                    {r.resid_bps != null ? bps(r.resid_bps) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.total_pnl_cents) }}>
                    {r.total_pnl_cents != null ? signedDollars(r.total_pnl_cents) : "—"}
                  </td>
                  <td style={S.tdR}>{pctOrDash(r.total_pct)}</td>
                </tr>
              ))}
              {/* Book totals row — only when there is a book to total */}
              {brief.book && (
                <tr>
                  <td style={S.tdTotal}>Book</td>
                  <td style={S.tdR} />
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.day_pnl_cents) }}>
                    {pct(brief.book.day_bps / 10000)}
                  </td>
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.day_pnl_cents) }}>
                    {signedDollars(brief.book.day_pnl_cents)}
                  </td>
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.day_bps) }}>
                    {bps(brief.book.day_bps)}
                  </td>
                  <td style={S.tdRTotal}>—</td>
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.total_pnl_cents) }}>
                    {signedDollars(brief.book.total_pnl_cents)}
                  </td>
                  <td style={S.tdRTotal}>
                    {brief.book.total_pct != null ? pct(brief.book.total_pct) : "—"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {/* Overnight tape — the open brief's §2 */}
      {overnightTape && overnightTape.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>
            Overnight tape
            {tapeIsSynthetic && (
              <span style={S.sectionNote}> · synthetic feed · not live prices</span>
            )}
          </h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Name</th>
                <th style={S.thR}>vs prior close</th>
              </tr>
            </thead>
            <tbody>
              {overnightTape.rows.map((r: Row) => (
                <tr key={r.symbol ?? r.label}>
                  <td style={S.td}>{r.label}</td>
                  <td style={{ ...S.tdR, ...signColor(r.overnight_pct ?? r.overnight_abs) }}>
                    {r.overnight_pct != null
                      ? pctOrDash(r.overnight_pct)
                      : `${fmtLevel(r.level)} ${signedAbs(r.overnight_abs)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {overnightTape.note && <p style={S.muted}>{overnightTape.note}</p>}
        </section>
      )}

      {/* Your names, pre-market — the open brief's §3 */}
      {premarket && premarket.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>Your names, pre-market</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Name</th>
                <th style={S.thR}>Pre</th>
                <th style={S.thR}>Gap/sh</th>
                <th style={S.thR}>Pre vol</th>
              </tr>
            </thead>
            <tbody>
              {premarket.rows.map((r: Row) => (
                <tr key={r.symbol}>
                  <td style={S.td}>
                    {r.symbol}
                    {r.why && (
                      <div style={{ ...S.muted, fontWeight: 400, margin: "2px 0 0" }}>{r.why}</div>
                    )}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.pre_pct) }}>{pctOrDash(r.pre_pct)}</td>
                  <td style={{ ...S.tdR, ...signColor(r.gap_cents) }}>
                    {dollarsOrDash(r.gap_cents)}
                  </td>
                  <td style={S.tdR}>{multOrDash(r.premarket_vol_mult)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {premarket.note && <p style={S.muted}>{premarket.note}</p>}
        </section>
      )}

      {/* On the clock today — the open brief's §4 */}
      {calendar && calendar.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>On the clock today</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>When</th>
                <th style={S.th}>What</th>
                <th style={S.th}>Whose</th>
              </tr>
            </thead>
            <tbody>
              {calendar.rows.map((r: Row) => (
                <tr key={`${r.occurs_at}-${r.label}`}>
                  <td style={S.td}>{r.occurs_at ?? "—"}</td>
                  <td style={{ ...S.td, fontWeight: 400 }}>{r.label}</td>
                  <td style={S.td}>
                    <span style={r.tag === "holding" ? S.tagStrong : S.tag}>{r.tag}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Sector setup — the open brief's §5 */}
      {sectors && sectors.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>Sector setup</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Sector</th>
                <th style={S.th}>Benchmark</th>
                <th style={S.thR}>5d</th>
                <th style={S.thR}>vs SPY</th>
                <th style={S.thR}>Pre-market</th>
              </tr>
            </thead>
            <tbody>
              {sectors.rows.map((r: Row) => (
                <tr key={r.sector_id ?? r.name}>
                  <td style={S.td}>{r.name}</td>
                  <td style={{ ...S.td, fontWeight: 400 }}>{r.benchmark_symbol ?? "—"}</td>
                  <td style={{ ...S.tdR, ...signColor(r.ret_5d) }}>{pctOrDash(r.ret_5d)}</td>
                  <td style={{ ...S.tdR, ...signColor(r.vs_spy_5d) }}>{pctOrDash(r.vs_spy_5d)}</td>
                  <td style={S.tdR}>{pctOrDash(r.premarket)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Exposure check — the flags' documented home (docs/05 §6) */}
      {brief.flags.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>Exposure check</h2>
          <ul style={S.claimList}>
            {brief.flags.map((f, i) => (
              <li key={`${f.type}-${f.symbol ?? f.sector_id ?? i}`} style={S.claimItem}>
                <span>
                  <strong>{f.type.replace(/_/g, " ")}</strong>
                  {f.symbol ? ` (${f.symbol})` : ""}
                </span>
                <span style={{ color: f.severity === "warn" ? "#8b2d2d" : "#666" }}>
                  {f.value != null ? f.value.toFixed(2) : f.severity}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {omitted.length > 0 && (
        <p style={S.muted}>
          {omitted.map((n) => (
            <span key={n} style={{ display: "block" }}>
              {n}
            </span>
          ))}
        </p>
      )}

      {/* Roll-up line — the suppressed names, folded into one line */}
      {brief.suppressed.length > 0 && (
        <p style={S.muted}>{brief.suppressed.join(", ")} — unchanged.</p>
      )}

      {/* How they traded — tape quality for the movers */}
      {tape && tape.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>How they traded</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Symbol</th>
                <th style={S.thR}>RVOL</th>
                <th style={S.th}>Range position</th>
              </tr>
            </thead>
            <tbody>
              {tape.rows.map((r: Row) => (
                <tr key={r.symbol}>
                  <td style={S.td}>{r.symbol}</td>
                  <td style={S.tdR}>{r.rvol != null ? `${r.rvol.toFixed(2)}×` : "—"}</td>
                  <td style={{ ...S.td, width: "55%" }}>
                    <RangeBar position={r.range_position} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Yesterday's flag, resolved — the accountability loop */}
      {brief.resolved_claims.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>Yesterday's flag, resolved</h2>
          <ul style={S.claimList}>
            {brief.resolved_claims.map((c: Claim) => (
              <li key={c.id} style={S.claimItem}>
                <span>
                  <strong>{c.symbol}</strong> {c.type.replace(/_/g, " ")} ({c.direction})
                </span>
                <span style={{ fontWeight: 700, ...signColor(c.outcome === "correct" ? 1 : -1) }}>
                  {c.outcome === "correct" ? "✓ correct" : "✗ wrong"}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(brief.data_quality.missing.length > 0 || brief.data_quality.stale.length > 0) && (
        <p style={S.muted}>
          {brief.data_quality.missing.length > 0 &&
            `missing: ${brief.data_quality.missing.join(", ")}. `}
          {brief.data_quality.stale.length > 0 && `stale: ${brief.data_quality.stale.join(", ")}.`}
        </p>
      )}
    </main>
  );
}

function RangeBar({ position }: { position: number | null | undefined }) {
  if (position == null) return <span style={S.muted}>—</span>;
  const pctFromLow = Math.round(position * 100);
  // Close near the high = strong (pine); near the low = weak (oxblood).
  const fill = position >= 0.5 ? "#1a6b43" : "#8b2d2d";
  return (
    <div style={S.barTrack} title={`close at ${pctFromLow}% of the day's range`}>
      <div style={{ ...S.barDot, left: `calc(${pctFromLow}% - 5px)`, background: fill }} />
    </div>
  );
}

function Stat({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return (
    <div>
      <div style={S.statLabel}>{label}</div>
      <div style={{ ...S.statValue, ...(positive == null ? {} : signColor(positive ? 1 : -1)) }}>
        {value}
      </div>
    </div>
  );
}

// --- formatters ---
function dollars(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function signedDollars(cents: number): string {
  const sign = cents >= 0 ? "+" : "-";
  return `${sign}$${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function bps(value: number): string {
  return `${value >= 0 ? "+" : ""}${value}bps`;
}
// U+2212 (minus sign), not ASCII "-" — see `signedAbs` below; `toFixed` would
// otherwise emit the ASCII glyph here while §2's level-quoted rows and §3's
// gap column use U+2212, splitting the sign glyph within the same row.
function pct(fraction: number): string {
  const sign = fraction >= 0 ? "+" : "−";
  return `${sign}${Math.abs(fraction * 100).toFixed(2)}%`;
}
function pctOrDash(fraction: number | null | undefined): string {
  return fraction == null ? "—" : pct(fraction);
}
function fmtLevel(level: number | null | undefined): string {
  return level == null ? "—" : level.toFixed(2);
}
// U+2212 (minus sign), not ASCII "-" — same convention as `dollarsOrDash`
// below, so the sign reads consistently within one row (docs/06).
function signedAbs(v: number | null | undefined): string {
  if (v == null) return "";
  const sign = v >= 0 ? "+" : "−";
  return `${sign}${Math.abs(v).toFixed(2)}`;
}
// The gap is dollars per share, not percent — that's the figure you act on
// (docs/01). Per-share, not per-position: the open brief carries no position
// data by design.
function dollarsOrDash(cents: number | null | undefined): string {
  if (cents == null) return "—";
  const sign = cents >= 0 ? "+" : "−";
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`;
}
function multOrDash(m: number | null | undefined): string {
  return m == null ? "—" : `${m.toFixed(1)}×`;
}
function signColor(v: number | null | undefined): React.CSSProperties {
  if (v == null || v === 0) return {};
  // pine / oxblood — the pair chosen for surviving dark-mode inversion (docs/06).
  return { color: v > 0 ? "#1a6b43" : "#8b2d2d" };
}

const S: Record<string, React.CSSProperties> = {
  main: { fontFamily: "system-ui", padding: "2rem", maxWidth: 820, margin: "0 auto" },
  crumb: { fontSize: "0.85rem", margin: "0 0 8px" },
  subject: { margin: "0 0 4px", fontSize: "1.35rem" },
  muted: { color: "#666", fontSize: "0.85rem", margin: "4px 0" },
  oneThing: {
    background: "#fff7d6",
    padding: "12px 14px",
    borderRadius: 8,
    margin: "14px 0",
    fontSize: "1rem",
  },
  card: { border: "1px solid #e2e2e2", borderRadius: 10, padding: "14px 16px", margin: "16px 0" },
  h2: { margin: "0 0 12px", fontSize: "1rem", color: "#333" },
  sectionNote: { fontWeight: 400, fontSize: "0.78rem", color: "#888" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 14 },
  statLabel: {
    fontSize: "0.72rem",
    color: "#888",
    textTransform: "uppercase",
    letterSpacing: "0.03em",
  },
  statValue: { fontSize: "1.05rem", fontWeight: 600, marginTop: 2 },
  barTrack: {
    position: "relative",
    height: 8,
    borderRadius: 4,
    background: "linear-gradient(90deg, #f0e6e6, #e8e8e8, #e6f0ea)",
    border: "1px solid #eee",
  },
  barDot: {
    position: "absolute",
    top: -2,
    width: 10,
    height: 10,
    borderRadius: "50%",
  },
  tag: {
    fontSize: "0.72rem",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    color: "#666",
    background: "#eee",
    padding: "2px 6px",
    borderRadius: 3,
  },
  tagStrong: {
    fontSize: "0.72rem",
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    color: "#fff",
    background: "#1B2A4A",
    padding: "2px 6px",
    borderRadius: 3,
  },
  claimList: { listStyle: "none", padding: 0, margin: 0, fontSize: "0.9rem" },
  claimItem: {
    display: "flex",
    justifyContent: "space-between",
    padding: "6px 0",
    borderBottom: "1px solid #f4f4f4",
  },
  table: { borderCollapse: "collapse", width: "100%", fontSize: "0.88rem" },
  th: {
    textAlign: "left",
    padding: "4px 8px 6px 0",
    color: "#888",
    fontWeight: 500,
    borderBottom: "1px solid #eee",
  },
  thR: {
    textAlign: "right",
    padding: "4px 0 6px 8px",
    color: "#888",
    fontWeight: 500,
    borderBottom: "1px solid #eee",
  },
  td: { padding: "5px 8px 5px 0", borderBottom: "1px solid #f4f4f4", fontWeight: 600 },
  tdR: {
    padding: "5px 0 5px 8px",
    borderBottom: "1px solid #f4f4f4",
    textAlign: "right",
    fontVariantNumeric: "tabular-nums",
  },
  tdTotal: { padding: "7px 8px 5px 0", borderTop: "2px solid #ddd", fontWeight: 700 },
  provMarker: { fontSize: "0.65rem", color: "#888", marginLeft: 2, fontWeight: 600 },
  tdRTotal: {
    padding: "7px 0 5px 8px",
    borderTop: "2px solid #ddd",
    textAlign: "right",
    fontWeight: 700,
    fontVariantNumeric: "tabular-nums",
  },
};
