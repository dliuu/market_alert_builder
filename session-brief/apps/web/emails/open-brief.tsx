import type { BriefObject, Flag, Row, Section } from "@/lib/contracts/brief";
import {
  Body,
  Container,
  Head,
  Hr,
  Html,
  Link,
  Preview,
  Section as Sec,
} from "@react-email/components";
import React from "react";
import { font, palette, signColor } from "./theme";

// The open email (M14) — the forward-looking one: what's on the clock, how the
// sectors are set up, where the exposure sits. Deliberately a second template
// rather than a branch in close-brief.tsx: "different jobs, different templates"
// (docs/01). There is no P&L here and no scorecard, because the open brief
// carries no `book` at all.
//
// §2 overnight tape and §3 pre-market names (M15) render their rows when the
// feed populated them; when assembly suppressed a section instead, the
// template renders its note rather than dropping it silently — the reader
// should know the brief is short by design.

const WIDTH = 600;

export function OpenBrief({ brief }: { brief: BriefObject }) {
  const section = (id: string) => brief.sections.find((s) => s.id === id);
  const tape = section("overnight_tape");
  const pre = section("premarket");
  const calendar = section("calendar");
  const sectors = section("sector_setup");
  const dateLong = formatDate(brief.session_date);
  const preheader = brief.one_thing ?? brief.subject;

  const omitted = [tape, pre]
    .filter((s): s is Section => s?.tier === "suppressed" && !!s.note && s.rows.length === 0)
    .filter((s) => s.id !== "premarket") // §3 renders its own roll-up above
    .map((s) => s.note);

  return (
    <Html lang="en">
      <Head />
      <Preview>{preheader}</Preview>
      <Body style={bodyStyle}>
        <Container style={containerStyle}>
          {/* masthead: Before the bell */}
          <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={mtop}>
            <tbody>
              <tr>
                <td style={{ verticalAlign: "bottom" }}>
                  <span style={mtopTitle}>
                    Before the <span style={{ color: palette.navy }}>bell</span>
                  </span>
                </td>
                <td style={{ verticalAlign: "bottom", textAlign: "right" }}>
                  <span style={mtopDate}>
                    {dateLong}
                    <br />
                    opens 09:30
                  </span>
                </td>
              </tr>
            </tbody>
          </table>

          {/* the one thing */}
          {brief.one_thing && (
            <Sec style={{ margin: "20px 0 6px" }}>
              <p style={eyebrow}>The one thing</p>
              <p style={oneThing}>
                <span style={highlight}>{brief.one_thing}</span>
              </p>
            </Sec>
          )}

          {/* overnight tape */}
          {tape && tape.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Overnight tape" note="vs prior close" />
              <Tape rows={tape.rows} />
              {tape.note && <p style={note}>{tape.note}</p>}
            </Sec>
          )}

          {/* your names, pre-market */}
          {pre && pre.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Your names, pre-market" note="vs prior close" />
              <Premarket rows={pre.rows} />
              {pre.note && <p style={note}>{pre.note}</p>}
            </Sec>
          )}
          {pre && pre.rows.length === 0 && pre.note && (
            <Sec style={sec}>
              <SectionHead title="Your names, pre-market" />
              <p style={note}>{pre.note}</p>
            </Sec>
          )}

          {/* on the clock today */}
          {calendar && calendar.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="On the clock today" note="next seven days" />
              <Calendar rows={calendar.rows} />
            </Sec>
          )}
          {calendar && calendar.rows.length === 0 && calendar.note && (
            <Sec style={sec}>
              <SectionHead title="On the clock today" />
              <p style={note}>{calendar.note}</p>
            </Sec>
          )}

          {/* sector setup */}
          {sectors && sectors.rows.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Sector setup" note="trailing five sessions" />
              <Sectors rows={sectors.rows} />
            </Sec>
          )}

          {/* exposure check */}
          {brief.flags.length > 0 && (
            <Sec style={sec}>
              <SectionHead title="Exposure check" />
              {brief.flags.map((f, i) => (
                <FlagRow key={`${f.type}-${f.symbol ?? f.sector_id ?? i}`} flag={f} />
              ))}
            </Sec>
          )}

          {/* what this brief doesn't have yet */}
          {omitted.length > 0 && (
            <p style={note}>
              {omitted.map((n) => (
                <span key={n} style={{ display: "block" }}>
                  {n}
                </span>
              ))}
            </p>
          )}

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
          <Sec>
            <p style={footer}>
              Not investment advice. Figures are computed from the prior close.
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
          </Sec>
        </Container>
      </Body>
    </Html>
  );
}

// ─── sub-components ───

function SectionHead({ title, note: hint }: { title: string; note?: string }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={secHeadTable}>
      <tbody>
        <tr>
          <td style={secHeadTitle}>{title}</td>
          {hint && <td style={secHeadNote}>{hint}</td>}
        </tr>
      </tbody>
    </table>
  );
}

// Two columns of pairs, as in the design reference: six macro lines read faster
// side by side than as a six-row list.
function Tape({ rows }: { rows: Row[] }) {
  const pairs: Row[][] = [];
  for (let i = 0; i < rows.length; i += 2) pairs.push(rows.slice(i, i + 2));
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <tbody>
        {pairs.map((pair) => (
          <tr key={pair.map((r) => r.symbol).join("-")}>
            {pair.map((r) => (
              <React.Fragment key={r.symbol}>
                <td style={{ ...tdL, width: "28%" }}>
                  <span style={sym}>{r.label}</span>
                </td>
                <td
                  style={{
                    ...tdR,
                    width: "22%",
                    color: signColor(r.overnight_pct ?? r.overnight_abs),
                  }}
                >
                  {r.overnight_pct != null
                    ? pctOrDash(r.overnight_pct)
                    : `${fmtLevel(r.level)} ${signedAbs(r.overnight_abs)}`}
                </td>
              </React.Fragment>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Premarket({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={{ ...thR, width: 62 }}>Pre</th>
          <th style={{ ...thR, width: 70 }}>Gap</th>
          <th style={{ ...thR, width: 66 }}>Pre vol</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              {r.why && <span style={why}>{r.why}</span>}
            </td>
            <td style={{ ...tdR, color: signColor(r.pre_pct) }}>{pctOrDash(r.pre_pct)}</td>
            <td style={{ ...tdR, color: signColor(r.gap_cents) }}>{dollarsOrDash(r.gap_cents)}</td>
            <td style={tdR}>{multOrDash(r.premarket_vol_mult)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Calendar({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={{ ...thL, width: 74 }}>When</th>
          <th style={thL}>What</th>
          <th style={{ ...thR, width: 84 }}>Whose</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.occurs_at}-${r.label}`}>
            <td style={{ ...tdL, fontFamily: font.mono, fontSize: 11, color: palette.ink2 }}>
              {shortDate(r.occurs_at)}
            </td>
            <td style={tdL}>{r.label}</td>
            <td style={{ ...tdR, fontSize: 11 }}>
              <Tag tag={r.tag} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// `holding` is the one that costs you money if you miss it, so it's the one
// that gets ink; macro and watchlist stay quiet.
function Tag({ tag }: { tag: Row["tag"] }) {
  if (!tag) return <span style={mut}>—</span>;
  const strong = tag === "holding";
  return (
    <span
      style={{
        fontFamily: font.mono,
        fontSize: 10,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        color: strong ? palette.white : palette.ink2,
        backgroundColor: strong ? palette.navy : palette.rule2,
        padding: "2px 6px",
      }}
    >
      {tag}
    </span>
  );
}

function Sectors({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Sector</th>
          <th style={{ ...thR, width: 70 }}>5d</th>
          <th style={{ ...thR, width: 84 }}>vs SPY</th>
          <th style={{ ...thR, width: 74 }}>Pre</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.sector_id ?? r.name}>
            <td style={tdL}>
              <span style={sym}>{r.name}</span>
              {r.benchmark_symbol && <span style={why}>{r.benchmark_symbol}</span>}
            </td>
            <td style={{ ...tdR, color: signColor(r.ret_5d) }}>{pctOrDash(r.ret_5d)}</td>
            <td style={{ ...tdR, color: signColor(r.vs_spy_5d) }}>{pctOrDash(r.vs_spy_5d)}</td>
            {/* null until the pre-market feed lands (M15) */}
            <td style={tdR}>{pctOrDash(r.premarket)}</td>
          </tr>
        ))}
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

// ─── formatters ───

function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}
function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    timeZone: "UTC",
  });
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
function fmtLevel(level: number | null | undefined): string {
  return level == null ? "—" : level.toFixed(2);
}
function signedAbs(v: number | null | undefined): string {
  return v == null ? "" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}
// The gap is dollars, not percent — that's the figure you act on (docs/01).
function dollarsOrDash(cents: number | null | undefined): string {
  if (cents == null) return "—";
  const sign = cents >= 0 ? "+" : "−";
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`;
}
function multOrDash(m: number | null | undefined): string {
  return m == null ? "—" : `${m.toFixed(1)}×`;
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
  fontSize: 11,
  color: palette.ink2,
  lineHeight: 1.5,
};
const eyebrow: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  color: palette.ink3,
  margin: "0 0 6px",
};
const oneThing: React.CSSProperties = {
  fontFamily: font.body,
  fontSize: 15,
  lineHeight: 1.55,
  margin: 0,
  color: palette.ink,
};
const highlight: React.CSSProperties = {
  backgroundColor: palette.mark,
  padding: "1px 2px",
};
const sec: React.CSSProperties = { margin: "22px 0 0" };
const secHeadTable: React.CSSProperties = {
  borderBottom: `1px solid ${palette.ink}`,
  paddingBottom: 4,
  marginBottom: 8,
};
const secHeadTitle: React.CSSProperties = {
  fontFamily: font.disp,
  fontSize: 13,
  fontWeight: 700,
  letterSpacing: "-0.01em",
};
const secHeadNote: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  color: palette.ink3,
  textAlign: "right",
};
const dataTable: React.CSSProperties = { borderCollapse: "collapse", width: "100%" };
const thL: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  letterSpacing: "0.04em",
  textTransform: "uppercase",
  color: palette.ink3,
  textAlign: "left",
  padding: "4px 6px 6px 0",
  fontWeight: 400,
};
const thR: React.CSSProperties = { ...thL, textAlign: "right", padding: "4px 0 6px 6px" };
const tdL: React.CSSProperties = {
  fontFamily: font.disp,
  fontSize: 13,
  padding: "7px 6px 7px 0",
  borderTop: `1px solid ${palette.rule2}`,
  verticalAlign: "top",
};
const tdR: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 12,
  padding: "7px 0 7px 6px",
  borderTop: `1px solid ${palette.rule2}`,
  textAlign: "right",
  verticalAlign: "top",
};
const sym: React.CSSProperties = { fontWeight: 700, letterSpacing: "-0.01em" };
const why: React.CSSProperties = {
  display: "block",
  fontFamily: font.mono,
  fontSize: 10,
  color: palette.ink3,
  marginTop: 2,
};
const mut: React.CSSProperties = { color: palette.ink3 };
const note: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 11,
  color: palette.ink3,
  margin: "12px 0 0",
  lineHeight: 1.6,
};
const flagBox: React.CSSProperties = {
  borderLeft: `3px solid ${palette.navy}`,
  backgroundColor: palette.card,
  padding: "8px 10px",
  margin: "8px 0 0",
};
const flagLabel: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  color: palette.ink3,
};
const flagText: React.CSSProperties = {
  fontFamily: font.body,
  fontSize: 13,
  lineHeight: 1.5,
  margin: "2px 0 0",
};
const footer: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: 10,
  lineHeight: 1.7,
  color: palette.ink3,
  margin: 0,
};
const footerLink: React.CSSProperties = { color: palette.ink2, textDecoration: "underline" };
