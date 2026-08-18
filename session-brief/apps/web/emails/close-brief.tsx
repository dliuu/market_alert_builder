import type { Book, BriefObject, Claim, Row } from "@/lib/contracts/brief";
import { Body, Container, Head, Hr, Html, Link, Preview, Section } from "@react-email/components";
import { font, palette, signColor } from "./theme";

// The close email, rendered from the BriefObject (docs/04). Everything here is
// substituted from computed fields — the only prose is `one_thing` and each row's
// `why`, both written by the narration stage. No figure is invented in the template.

const WIDTH = 600;

// Configured by `emails/cn/close-brief.tsx` (D32): every default here
// reproduces the US template's output byte-for-byte. CN specifics (¥, "vs CSI
// 300", the CN kind label) are set only by that wrapper — never here.
export type CloseBriefOptions = {
  currencySymbol?: string;
  benchmarkLabel?: string;
  kindLabel?: string;
  closeLabel?: string;
};

export function CloseBrief({
  brief,
  options,
}: { brief: BriefObject; options?: CloseBriefOptions }) {
  const currencySymbol = options?.currencySymbol ?? "$";
  const benchmarkLabel = options?.benchmarkLabel ?? "vs SPY";
  const kindLabel = options?.kindLabel;
  const closeLabel = options?.closeLabel ?? "session closed 16:00";
  const attribution = brief.sections.find((s) => s.id === "attribution");
  const tape = brief.sections.find((s) => s.id === "tape_quality");
  const catalysts = brief.sections.find((s) => s.id === "catalysts");
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
                    {kindLabel && `${kindLabel} · `}
                    {dateLong}
                    <br />
                    {closeLabel}
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
              <Scorecard
                book={brief.book}
                positions={attribution?.rows.length ?? 0}
                currencySymbol={currencySymbol}
                benchmarkLabel={benchmarkLabel}
              />
            </Section>
          )}

          {/* attribution */}
          {brief.book && attribution && attribution.rows.length > 0 && (
            <Section style={sec}>
              <SectionHead title="Attribution" note="contribution to book return" />
              <Attribution
                rows={attribution.rows}
                book={brief.book}
                currencySymbol={currencySymbol}
              />
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

          {/* catalysts (M17) — the supply-side events the price series can't
              explain. Rows arrive one per signal, already ordered by severity
              and already decayed; the template groups by symbol and never
              re-ranks or re-suppresses (D16). */}
          {catalysts && catalysts.rows.length > 0 && (
            <Section style={sec}>
              <SectionHead title="Catalysts" note="insider flow and proposed supply" />
              <Catalysts rows={catalysts.rows} currencySymbol={currencySymbol} />
            </Section>
          )}

          {/* explicit absence is information, and it's cheap */}
          {catalysts?.note && <p style={note}>{catalysts.note}.</p>}

          {/* No exposure check here since M14: §6 is the open brief's section
              (docs/05), and the weekly flag cap is one clock that cannot serve
              both briefs. `brief.flags` is empty on a close brief. */}

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

function Scorecard({
  book,
  positions,
  currencySymbol,
  benchmarkLabel,
}: { book: Book; positions: number; currencySymbol: string; benchmarkLabel: string }) {
  const cells = [
    {
      k: "Session",
      v: signedPct(book.day_bps / 100),
      color: signColor(book.day_pnl_cents),
      s: signedDollars(book.day_pnl_cents, currencySymbol),
    },
    {
      k: benchmarkLabel,
      v: book.vs_spy_bps != null ? bps(book.vs_spy_bps) : "—",
      color: book.vs_spy_bps != null ? signColor(book.vs_spy_bps) : palette.ink3,
      s: "20-session",
    },
    {
      k: "Book value",
      v: dollars(book.value_cents, currencySymbol),
      color: palette.ink,
      s: `${positions} positions`,
    },
    {
      k: "Unrealised",
      v: signedDollars(book.total_pnl_cents, currencySymbol),
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

function Attribution({
  rows,
  book,
  currencySymbol,
}: { rows: Row[]; book: Book; currencySymbol: string }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={thR}>Close</th>
          <th style={thR}>Day</th>
          <th style={thR}>Day P&amp;L</th>
          <th style={thR}>bps</th>
          <th style={thR}>Resid</th>
          <th style={thR}>Total P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              {r.provisional && (
                <sup style={provMarker} title="provisional — not yet reconciled with fills">
                  p
                </sup>
              )}
              {r.why && <span style={why}>{r.why}</span>}
            </td>
            <td style={tdR}>{r.close != null ? r.close.toFixed(2) : "—"}</td>
            <td style={{ ...tdR, color: signColor(r.day_return) }}>{pctOrDash(r.day_return)}</td>
            <td style={{ ...tdR, color: signColor(r.day_pnl_cents) }}>
              {r.day_pnl_cents != null ? signedDollarsRound(r.day_pnl_cents, currencySymbol) : "—"}
            </td>
            <td style={{ ...tdR, color: signColor(r.contribution_bps) }}>
              {r.contribution_bps != null ? signedInt(r.contribution_bps) : "—"}
            </td>
            <td style={{ ...tdR, color: signColor(r.resid_bps) }}>
              {r.resid_bps != null ? signedInt(r.resid_bps) : "—"}
            </td>
            <td style={{ ...tdR, color: signColor(r.total_pnl_cents) }}>
              {r.total_pnl_cents != null
                ? signedDollarsRound(r.total_pnl_cents, currencySymbol)
                : "—"}
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
            {signedDollarsRound(book.day_pnl_cents, currencySymbol)}
          </td>
          <td style={{ ...totR, color: signColor(book.day_bps) }}>{signedInt(book.day_bps)}</td>
          <td style={totR}>—</td>
          <td style={{ ...totR, color: signColor(book.total_pnl_cents) }}>
            {signedDollarsRound(book.total_pnl_cents, currencySymbol)}
            {book.total_pct != null && <span style={mut}> {signedPct(book.total_pct * 100)}</span>}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

// §4 carries every owned name since M19, quiet ones included — it answers
// "where does each position stand", which a flat name has an answer to.
//
// Six columns is the most 600px will take, so the level evidence rides a muted
// sub-line under the symbol rather than four more columns. The full snapshot —
// all three MA distances, both touch counts, the 52-week pair — is on the web
// archive, which has no width to run out of.
function Tape({ rows }: { rows: Row[] }) {
  return (
    <table role="presentation" width="100%" cellPadding={0} cellSpacing={0} style={dataTable}>
      <thead>
        <tr>
          <th style={thL}>Name</th>
          <th style={thR}>RVOL</th>
          <th style={thR}>Vol wk</th>
          <th style={thR}>Vol mo</th>
          <th style={thR}>vs 50d</th>
          <th style={{ ...thR, width: 110 }}>Close in range</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.symbol}>
            <td style={tdL}>
              <span style={sym}>{r.symbol}</span>
              <Breakout kind={r.breakout} />
              {r.why && <span style={why}>{r.why}</span>}
              <Levels row={r} />
            </td>
            <td style={tdR}>{multiple(r.rvol)}</td>
            <td style={tdR}>{multiple(r.vol_vs_5d)}</td>
            <td style={tdR}>{multiple(r.vol_vs_21d)}</td>
            <td style={{ ...tdR, color: signColor(r.ma50_dist) }}>{pctOrDash(r.ma50_dist)}</td>
            <td style={{ ...tdR, verticalAlign: "middle" }}>
              <RangeBar position={r.range_position} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// A confirmed close through a tested level on unusual volume. Assembly decides
// whether this fired; the renderer only reads it.
function Breakout({ kind }: { kind: Row["breakout"] }) {
  if (kind == null) return null;
  const up = kind === "up";
  return (
    <span style={{ ...breakoutBadge, color: up ? palette.pine : palette.ox }}>
      {up ? "▲ broke out" : "▼ broke down"}
    </span>
  );
}

// Support and resistance with the evidence attached. A level without its touch
// count is a line on a chart; with it, it is a claim you can check.
function Levels({ row }: { row: Row }) {
  const parts: string[] = [];
  if (row.support != null) {
    parts.push(
      `Support ${price(row.support)}${evidence(row.support_touches, row.support_last_touch)}`,
    );
  }
  if (row.resistance != null) {
    parts.push(
      `Resistance ${price(row.resistance)}${evidence(row.resistance_touches, row.resistance_last_touch)}`,
    );
  }
  if (row.ma_stack != null) parts.push(`MAs ${row.ma_stack}`);
  if (parts.length === 0) return null;
  return <span style={levelLine}>{parts.join("  ·  ")}</span>;
}

function evidence(touches: number | null | undefined, last: string | null | undefined): string {
  if (touches == null) return "";
  const when = last ? `, last ${shortDate(last)}` : "";
  return ` (${touches}×${when})`;
}

// Catalysts, grouped by symbol. A `full`-tier signal gets its figures; a
// `brief`-tier one (second sighting, or an inherently minor signal) is
// condensed to a single line. Assembly decided which is which — the tier is
// read here, never computed.
function Catalysts({ rows, currencySymbol }: { rows: Row[]; currencySymbol: string }) {
  const symbols: string[] = [];
  for (const r of rows) {
    const s = r.symbol ?? "—";
    if (!symbols.includes(s)) symbols.push(s);
  }

  return (
    <>
      {symbols.map((symbol) => {
        const mine = rows.filter((r) => (r.symbol ?? "—") === symbol);
        const lead = mine.some((r) => r.tier === "full");
        return (
          <table
            key={symbol}
            role="presentation"
            width="100%"
            cellPadding={0}
            cellSpacing={0}
            style={dataTable}
          >
            <tbody>
              <tr>
                <td style={tdL}>
                  <span style={sym}>
                    {lead && <span style={{ color: palette.ox }}>⚠ </span>}
                    {symbol}
                  </span>
                  {mine.map((r, i) => (
                    <span key={`${r.kind}-${r.ref_date}-${i}`} style={why}>
                      {catalystLine(r, currencySymbol)}
                      {r.why && ` — ${r.why}`}
                    </span>
                  ))}
                </td>
              </tr>
            </tbody>
          </table>
        );
      })}
    </>
  );
}

// One line of prose-free fact per signal. Every figure comes from the object;
// nothing here is computed and nothing is inferred from a missing value — an
// unknown size renders as "size unknown", which is information (open q. 4).
function catalystLine(r: Row, currencySymbol: string): string {
  const on = r.ref_date ? shortDate(r.ref_date) : "";
  const suffix = r.ambiguous_code ? " [type unclassified]" : "";

  switch (r.kind) {
    case "clevel_buy":
      return `Officer purchase — ${dollars(r.value_cents ?? 0, currencySymbol)}, ${on}${suffix}`;
    case "cluster":
      return `Insider cluster — ${r.insider_count ?? 0} insiders, ${dollars(
        r.value_cents ?? 0,
        currencySymbol,
      )}, ${on}${suffix}`;
    case "pre_earnings":
      return `Sale ${r.days_to_event ?? 0} sessions pre-earnings — ${dollars(
        r.value_cents ?? 0,
        currencySymbol,
      )}${suffix}`;
    case "outsized_sale":
      return `Sold ${proportion(r.pct_of_holding)} of holding, ${on}${suffix}`;
    case "cadence_break":
      return `Off usual cadence — ${dollars(r.value_cents ?? 0, currencySymbol)}, ${on}${suffix}`;
    case "large_144":
    case "standard_144":
      return `Form 144 — ${shares(r.shares)} proposed (${
        r.pct_of_float != null ? `${proportion(r.pct_of_float)} of float` : "size unknown"
      }), ${on}`;
    case "unconverted_144":
      return `Form 144 unexecuted after ${r.days_outstanding ?? 0} days — ${shares(
        r.shares,
      )}, ${on}`;
    default:
      return `${r.kind ?? "signal"} ${on}`;
  }
}

function shares(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M shares`;
  if (v >= 1_000) return `${Math.round(v / 1_000)}K shares`;
  return `${Math.round(v)} shares`;
}

function shortDate(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${m}/${d}`;
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
function dollars(cents: number, symbol = "$"): string {
  return `${symbol}${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}
function signedDollars(cents: number, symbol = "$"): string {
  const s = cents >= 0 ? "+" : "-";
  return `${s}${symbol}${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function signedDollarsRound(cents: number, symbol = "$"): string {
  const s = cents >= 0 ? "+" : "-";
  return `${s}${symbol}${Math.round(Math.abs(cents) / 100).toLocaleString("en-US")}`;
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

function multiple(v: number | null | undefined): string {
  return v == null ? "—" : `${v.toFixed(1)}×`;
}

function price(v: number | null | undefined): string {
  return v == null ? "—" : v.toFixed(2);
}

// A proportion, not a change: "45% of holding" takes no sign. Signing it would
// read as a move of +45%, which is a different and much more alarming claim.
function proportion(fraction: number | null | undefined): string {
  return fraction == null ? "—" : `${(fraction * 100).toFixed(fraction < 0.01 ? 2 : 1)}%`;
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
const breakoutBadge: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  paddingLeft: 6,
  whiteSpace: "nowrap",
};
const levelLine: React.CSSProperties = {
  display: "block",
  fontSize: 11,
  color: palette.ink3,
  paddingTop: 2,
};
const provMarker: React.CSSProperties = {
  fontSize: 8,
  color: palette.ink3,
  marginLeft: 2,
  fontWeight: 600,
};
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
