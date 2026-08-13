import type { Book, BriefObject, Claim, Flag, Row } from "@/lib/contracts/brief";
import { Body, Container, Head, Hr, Html, Link, Preview, Section } from "@react-email/components";
import { font, palette, signColor } from "./theme";

// The close email, rendered from the BriefObject (docs/04). Everything here is
// substituted from computed fields — the only prose is `one_thing` and each row's
// `why`, both written by the narration stage. No figure is invented in the template.

const WIDTH = 600;

export function CloseBrief({ brief }: { brief: BriefObject }) {
  const attribution = brief.sections.find((s) => s.id === "attribution");
  const tape = brief.sections.find((s) => s.id === "tape_quality");
  const dateLong = formatDate(brief.session_date);
  const preheader = brief.one_thing ?? brief.subject;

  return (
    <Html lang="en">
      <Head />
      <Preview>{preheader}</Preview>
      <Body style={bodyStyle}>
        <Container style={containerStyle}>
          {/* masthead: After the bell */}
          <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={mtop}>
            <tbody>
              <tr>
                <td style={{ verticalAlign: "bottom" }}>
                  <span style={mtopTitle}>
                    After the <span style={{ color: palette.ox }}>bell</span>
                  </span>
                </td>
                <td style={{ verticalAlign: "bottom", textAlign: "right" }}>
                  <span style={mtopDate}>
                    {dateLong}
                    <br />
                    session closed 16:00
                  </span>
                </td>
              </tr>
            </tbody>
          </table>

          {/* the one thing */}
          {brief.one_thing && (
            <Section style={{ margin: "20px 0 6px" }}>
              <p style={eyebrow}>The one thing</p>
              <p style={oneThing}>
                <span style={highlight}>{brief.one_thing}</span>
              </p>
            </Section>
          )}

          {/* session scorecard — `book` is nullable since v3 (the open brief
              omits it), but a close brief always has one. Guard once here. */}
          {brief.book && (
            <Section style={sec}>
              <SectionHead title="Session scorecard" />
              <Scorecard book={brief.book} positions={attribution?.rows.length ?? 0} />
            </Section>
          )}

          {/* attribution */}
          {brief.book && attribution && attribution.rows.length > 0 && (
            <Section style={sec}>
              <SectionHead title="Attribution" note="contribution to book return" />
              <Attribution rows={attribution.rows} book={brief.book} />
            </Section>
          )}

          {/* roll-up: the suppressed names, folded into one line */}
          {brief.suppressed.length > 0 && (
            <p style={note}>{brief.suppressed.join(", ")} — unchanged.</p>
          )}

          {/* how they traded */}
          {tape && tape.rows.length > 0 && (
            <Section style={sec}>
              <SectionHead title="How they traded" note="tape quality, not just direction" />
              <Tape rows={tape.rows} />
            </Section>
          )}

          {/* exposure check */}
          {brief.flags.length > 0 && (
            <Section style={sec}>
              <SectionHead title="Exposure check" />
              {brief.flags.map((f, i) => (
                <FlagRow key={`${f.type}-${i}`} flag={f} />
              ))}
            </Section>
          )}

          {/* yesterday's flag, resolved */}
          {brief.resolved_claims.length > 0 && (
            <Section style={sec}>
              <SectionHead title="Yesterday's flag, resolved" />
              {brief.resolved_claims.map((c) => (
                <ResolvedRow key={c.id} claim={c} />
              ))}
            </Section>
          )}

          {/* data quality */}
          {(brief.data_quality.missing.length > 0 || brief.data_quality.stale.length > 0) && (
            <p style={note}>
              {brief.data_quality.missing.length > 0 &&
                `missing: ${brief.data_quality.missing.join(", ")}. `}
              {brief.data_quality.stale.length > 0 &&
                `stale: ${brief.data_quality.stale.join(", ")}.`}
            </p>
          )}

          {/* footer */}
          <Hr style={{ borderColor: palette.rule, margin: "26px 0 14px" }} />
          <Section>
            <p style={footer}>
              Not investment advice. Figures are computed from end-of-day data.
              <br />
              <Link href="{{DASHBOARD_URL}}" style={footerLink}>
                Open the dashboard
              </Link>{" "}
              ·{" "}
              <Link href="{{UNSUBSCRIBE_URL}}" style={footerLink}>
                Unsubscribe
              </Link>
              <br />
              Session Brief · {"{{MAILING_ADDRESS}}"}
            </p>
          </Section>
        </Container>
      </Body>
    </Html>
  );
}

// ─── sub-components ───

function SectionHead({ title, note }: { title: string; note?: string }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={secHeadTable}>
      <tbody>
        <tr>
          <td style={secHeadTitle}>{title}</td>
          {note && <td style={secHeadNote}>{note}</td>}
        </tr>
      </tbody>
    </table>
  );
}

function Scorecard({ book, positions }: { book: Book; positions: number }) {
  const cells = [
    {
      k: "Session",
      v: signedPct(book.day_bps / 100),
      color: signColor(book.day_pnl_cents),
      s: signedDollars(book.day_pnl_cents),
    },
    {
      k: "vs SPY",
      v: book.vs_spy_bps != null ? bps(book.vs_spy_bps) : "—",
      color: book.vs_spy_bps != null ? signColor(book.vs_spy_bps) : palette.ink3,
      s: "20-session",
    },
    {
      k: "Book value",
      v: dollars(book.value_cents),
      color: palette.ink,
      s: `${positions} positions`,
    },
    {
      k: "Unrealised",
      v: signedDollars(book.total_pnl_cents),
      color: signColor(book.total_pnl_cents),
      s: book.total_pct != null ? signedPct(book.total_pct * 100) : "on cost",
    },
  ];
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={scorecard}>
      <tbody>
        <tr>
          {cells.map((c) => (
            <td key={c.k} style={scoreCell}>
              <div style={scoreK}>{c.k}</div>
              <div style={{ ...scoreV, color: c.color }}>{c.v}</div>
              <div style={scoreS}>{c.s}</div>
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  );
}

function Attribution({ rows, book }: { rows: Row[]; book: Book }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={thR}>Close</th>
          <th style={thR}>Day</th>
          <th style={thR}>Day P&amp;L</th>
          <th style={thR}>bps</th>
          <th style={thR}>Total P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              {r.why && <span style={why}>{r.why}</span>}
            </td>
            <td style={tdR}>{r.close != null ? r.close.toFixed(2) : "—"}</td>
            <td style={{ ...tdR, color: signColor(r.day_return) }}>{pctOrDash(r.day_return)}</td>
            <td style={{ ...tdR, color: signColor(r.day_pnl_cents) }}>
              {r.day_pnl_cents != null ? signedDollarsRound(r.day_pnl_cents) : "—"}
            </td>
            <td style={{ ...tdR, color: signColor(r.contribution_bps) }}>
              {r.contribution_bps != null ? signedInt(r.contribution_bps) : "—"}
            </td>
            <td style={{ ...tdR, color: signColor(r.total_pnl_cents) }}>
              {r.total_pnl_cents != null ? signedDollarsRound(r.total_pnl_cents) : "—"}
              {r.total_pct != null && <span style={mut}> {signedPct(r.total_pct * 100)}</span>}
            </td>
          </tr>
        ))}
        <tr>
          <td style={totL}>Book</td>
          <td style={totR}>—</td>
          <td style={{ ...totR, color: signColor(book.day_pnl_cents) }}>
            {signedPct(book.day_bps / 100)}
          </td>
          <td style={{ ...totR, color: signColor(book.day_pnl_cents) }}>
            {signedDollarsRound(book.day_pnl_cents)}
          </td>
          <td style={{ ...totR, color: signColor(book.day_bps) }}>{signedInt(book.day_bps)}</td>
          <td style={{ ...totR, color: signColor(book.total_pnl_cents) }}>
            {signedDollarsRound(book.total_pnl_cents)}
            {book.total_pct != null && <span style={mut}> {signedPct(book.total_pct * 100)}</span>}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

function Tape({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={thR}>RVOL</th>
          <th style={{ ...thR, width: 150 }}>Close in range</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              {r.why && <span style={why}>{r.why}</span>}
            </td>
            <td style={tdR}>{r.rvol != null ? `${r.rvol.toFixed(1)}×` : "—"}</td>
            <td style={{ ...tdR, verticalAlign: "middle" }}>
              <RangeBar position={r.range_position} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Range bar as a two-cell table (email-safe): a proportional fill, pine when the
// close sits in the top half of the day's range, oxblood when in the bottom.
function RangeBar({ position }: { position: number | null | undefined }) {
  if (position == null) return <span style={mut}>—</span>;
  const pct = Math.max(0, Math.min(100, Math.round(position * 100)));
  const fill = position >= 0.5 ? palette.pine : palette.ox;
  return (
    <table
      role="presentation"
      cellPadding={0}
      cellSpacing={0}
      style={{ display: "inline-block", width: 96, verticalAlign: "middle" }}
    >
      <tbody>
        <tr>
          {/* bgcolor attribute (via spread — not in React's TS types) is what
              Outlook honours; the CSS background is the fallback. */}
          <td
            width={`${pct}%`}
            {...{ bgcolor: fill }}
            style={{ ...barCell, backgroundColor: fill }}
          />
          <td
            width={`${100 - pct}%`}
            {...{ bgcolor: palette.rule }}
            style={{ ...barCell, backgroundColor: palette.rule }}
          />
        </tr>
      </tbody>
    </table>
  );
}

function FlagRow({ flag }: { flag: Flag }) {
  const label = flag.type.replace(/_/g, " ");
  const valueStr = flag.value != null ? ` — ${round2(flag.value)}` : "";
  const suffix = flag.symbol ? ` (${flag.symbol})` : "";
  return (
    <div style={flagBox}>
      <div style={flagLabel}>{label}</div>
      <p style={flagText}>
        {capitalize(label)}
        {suffix}
        {valueStr}.
      </p>
    </div>
  );
}

function ResolvedRow({ claim }: { claim: Claim }) {
  const correct = claim.outcome === "correct";
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={resolvedRow}>
      <tbody>
        <tr>
          <td style={{ fontFamily: font.mono, fontSize: 12, color: palette.ink }}>
            <span style={sym}>{claim.symbol}</span> {claim.type.replace(/_/g, " ")} (
            {claim.direction})
          </td>
          <td
            style={{
              fontFamily: font.mono,
              fontSize: 12,
              fontWeight: 700,
              textAlign: "right",
              color: correct ? palette.pine : palette.ox,
            }}
          >
            {correct ? "✓ correct" : "✗ wrong"}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

// ─── formatters ───

function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}
function dollars(cents: number): string {
  return `$${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}
function signedDollars(cents: number): string {
  const s = cents >= 0 ? "+" : "-";
  return `${s}$${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function signedDollarsRound(cents: number): string {
  const s = cents >= 0 ? "+" : "-";
  return `${s}$${Math.round(Math.abs(cents) / 100).toLocaleString("en-US")}`;
}
function bps(v: number): string {
  return `${v >= 0 ? "+" : ""}${v}bp`;
}
function signedInt(v: number): string {
  return `${v >= 0 ? "+" : ""}${v}`;
}
function signedPct(pct: number): string {
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
}
function pctOrDash(fraction: number | null | undefined): string {
  return fraction == null ? "—" : signedPct(fraction * 100);
}
function round2(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}
function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ─── styles ───

const bodyStyle: React.CSSProperties = {
  backgroundColor: palette.paper,
  margin: 0,
  padding: "16px 0",
  fontFamily: font.disp,
  color: palette.ink,
};
const containerStyle: React.CSSProperties = {
  width: WIDTH,
  maxWidth: "100%",
  margin: "0 auto",
  backgroundColor: palette.white,
  border: `1px solid ${palette.rule}`,
  padding: "24px 22px 30px",
};
const mtop: React.CSSProperties = { borderBottom: `2px solid ${palette.ink}`, paddingBottom: 10 };
const mtopTitle: React.CSSProperties = {
  fontFamily: font.disp,
  fontSize: 23,
  fontWeight: 700,
  letterSpacing: "-0.03em",
};
const mtopDate: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  letterSpacing: "0.1em",
  textTransform: "uppercase",
  color: palette.ink3,
  lineHeight: 1.6,
};
const eyebrow: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 9.5,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: palette.ink3,
  margin: "0 0 8px",
};
const oneThing: React.CSSProperties = {
  fontFamily: font.body,
  fontSize: 18,
  lineHeight: 1.5,
  margin: 0,
  color: palette.ink,
};
const highlight: React.CSSProperties = {
  // solid bgcolor fallback for the highlighter gradient (docs/06)
  backgroundColor: palette.mark,
  padding: "1px 2px",
};
const sec: React.CSSProperties = { marginTop: 26 };
const secHeadTable: React.CSSProperties = {
  borderBottom: `1px solid ${palette.rule}`,
  marginBottom: 10,
  paddingBottom: 6,
};
const secHeadTitle: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: palette.ink,
};
const secHeadNote: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  color: palette.ink3,
  textAlign: "right",
};
const note: React.CSSProperties = {
  fontFamily: font.body,
  fontSize: 14,
  lineHeight: 1.55,
  color: palette.ink2,
  margin: "10px 0 0",
};
const scorecard: React.CSSProperties = { borderCollapse: "separate", borderSpacing: 0 };
const scoreCell: React.CSSProperties = {
  width: "25%",
  backgroundColor: palette.white,
  border: `1px solid ${palette.rule2}`,
  padding: "11px 10px",
  verticalAlign: "top",
};
const scoreK: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 8.5,
  letterSpacing: "0.11em",
  textTransform: "uppercase",
  color: palette.ink3,
  marginBottom: 5,
};
const scoreV: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 17,
  fontWeight: 600,
  letterSpacing: "-0.02em",
};
const scoreS: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 9.5,
  color: palette.ink3,
  marginTop: 2,
};
const dataTable: React.CSSProperties = {
  borderCollapse: "collapse",
  fontFamily: font.mono,
  fontSize: 11.5,
};
const thBase: React.CSSProperties = {
  fontWeight: 500,
  fontSize: 9,
  letterSpacing: "0.1em",
  textTransform: "uppercase",
  color: palette.ink3,
  padding: "0 0 6px",
  borderBottom: `1px solid ${palette.rule2}`,
};
const thL: React.CSSProperties = { ...thBase, textAlign: "left" };
const thR: React.CSSProperties = { ...thBase, textAlign: "right", paddingLeft: 8 };
const tdBase: React.CSSProperties = {
  padding: "7px 0",
  borderBottom: `1px solid ${palette.rule2}`,
  verticalAlign: "top",
};
const tdL: React.CSSProperties = { ...tdBase, textAlign: "left" };
const tdR: React.CSSProperties = { ...tdBase, textAlign: "right", paddingLeft: 8 };
const totBase: React.CSSProperties = {
  padding: "9px 0 0",
  borderTop: `2px solid ${palette.ink}`,
  fontWeight: 600,
};
const totL: React.CSSProperties = {
  ...totBase,
  textAlign: "left",
  fontSize: 9,
  letterSpacing: "0.11em",
  textTransform: "uppercase",
  color: palette.ink3,
};
const totR: React.CSSProperties = { ...totBase, textAlign: "right", paddingLeft: 8 };
const sym: React.CSSProperties = { fontWeight: 600, fontSize: 12 };
const mut: React.CSSProperties = { color: palette.ink3 };
const why: React.CSSProperties = {
  display: "block",
  fontFamily: font.body,
  fontSize: 12.5,
  color: palette.ink2,
  marginTop: 2,
  lineHeight: 1.4,
  fontWeight: 400,
};
const barCell: React.CSSProperties = { height: 7, fontSize: 0, lineHeight: "0px" };
const flagBox: React.CSSProperties = {
  borderLeft: `3px solid ${palette.ox}`,
  padding: "10px 0 10px 13px",
  marginTop: 12,
  backgroundColor: "#FBF7F6",
};
const flagLabel: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 9.5,
  letterSpacing: "0.13em",
  textTransform: "uppercase",
  color: palette.ox,
  marginBottom: 5,
  fontWeight: 600,
};
const flagText: React.CSSProperties = {
  margin: 0,
  fontFamily: font.body,
  fontSize: 14,
  lineHeight: 1.5,
  color: palette.ink,
};
const resolvedRow: React.CSSProperties = {
  padding: "6px 0",
  borderBottom: `1px solid ${palette.rule2}`,
};
const footer: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 9.5,
  lineHeight: 1.7,
  color: palette.ink3,
  margin: 0,
};
const footerLink: React.CSSProperties = { color: palette.ink2 };
