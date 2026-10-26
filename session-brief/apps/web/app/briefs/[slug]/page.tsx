import { DEV_USER_ID } from "@/lib/constants";
import type { BriefObject, CallQuote, Claim, Row } from "@/lib/contracts/brief";
import { db } from "@/lib/db";
import { briefs } from "@/lib/db/schema";
import { and, eq } from "drizzle-orm";
import { notFound } from "next/navigation";
import { Fragment } from "react";

export const dynamic = "force-dynamic";

// Slug is "<YYYY-MM-DD>-<kind>", e.g. 2026-08-11-close.
const SLUG = /^(\d{4}-\d{2}-\d{2})-(open|close|open_cn|close_cn)$/;

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
  // Absent `currency` means USD (docs/04) — map to the display symbol once,
  // here, rather than re-deriving it at every money call site.
  const currencySymbol = brief.currency === "CNY" ? "¥" : "$";
  // Mirrors the email templates' `benchmarkLabel` (docs/04): the same figure
  // is CSI 300 on the CN book, SPY on the US one.
  const benchmarkLabel = brief.currency === "CNY" ? "vs CSI 300" : "vs SPY";
  const attribution = brief.sections.find((s) => s.id === "attribution");
  const tape = brief.sections.find((s) => s.id === "tape_quality");
  const catalysts = brief.sections.find((s) => s.id === "catalysts");
  const standing = brief.sections.find((s) => s.id === "standing");
  const premium = brief.sections.find((s) => s.id === "premium");
  // Same keys as the close email's §6 head note (`assemble` appends them).
  const premiumIsSynthetic = brief.data_quality.stale.includes("premium.synthetic");
  const premiumIsStale = brief.data_quality.stale.includes("premium.stale");
  // IV30 may stand bare here (never in the email), but marked: a dagger
  // and a footnote whenever a row's percentile is still pending.
  const premiumPending = premium?.rows.some((r) => r.iv30 != null && r.iv30_pctile == null);
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
            <Stat label="Book value" value={dollars(brief.book.value_cents, currencySymbol)} />
            <Stat
              label="Day P&L"
              value={`${signedDollars(brief.book.day_pnl_cents, currencySymbol)} · ${bps(brief.book.day_bps)}`}
              positive={brief.book.day_pnl_cents >= 0}
            />
            <Stat
              label="Total P&L"
              value={`${signedDollars(brief.book.total_pnl_cents, currencySymbol)}${
                brief.book.total_pct != null ? ` · ${pct(brief.book.total_pct)}` : ""
              }`}
              positive={brief.book.total_pnl_cents >= 0}
            />
            {brief.book.vs_spy_bps != null && (
              <Stat
                label={benchmarkLabel}
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
                  <td style={S.tdR}>
                    {r.close != null ? `${currencySymbol}${r.close.toFixed(2)}` : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.day_return) }}>
                    {pctOrDash(r.day_return)}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.day_pnl_cents) }}>
                    {r.day_pnl_cents != null ? signedDollars(r.day_pnl_cents, currencySymbol) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.contribution_bps) }}>
                    {r.contribution_bps != null ? bps(r.contribution_bps) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.resid_bps) }}>
                    {r.resid_bps != null ? bps(r.resid_bps) : "—"}
                  </td>
                  <td style={{ ...S.tdR, ...signColor(r.total_pnl_cents) }}>
                    {r.total_pnl_cents != null
                      ? signedDollars(r.total_pnl_cents, currencySymbol)
                      : "—"}
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
                    {signedDollars(brief.book.day_pnl_cents, currencySymbol)}
                  </td>
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.day_bps) }}>
                    {bps(brief.book.day_bps)}
                  </td>
                  <td style={S.tdRTotal}>—</td>
                  <td style={{ ...S.tdRTotal, ...signColor(brief.book.total_pnl_cents) }}>
                    {signedDollars(brief.book.total_pnl_cents, currencySymbol)}
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
                    {/* VIX's own-year standing at the prior close (M25). */}
                    {r.level_pctile != null && (
                      <span style={S.muted}> · {ordinalOrDash(r.level_pctile)} pctile</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {overnightTape.note && <p style={S.muted}>{overnightTape.note}</p>}
        </section>
      )}

      {/* Premium regime changes since the prior close — the open brief's strip
          under §2 (M25). Present only when some name's regime changed. */}
      {premium && brief.kind === "open" && (
        <section style={S.card}>
          <h2 style={S.h2}>
            Premium
            <span style={S.sectionNote}> · regime changes since the prior close</span>
          </h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Symbol</th>
                <th style={S.th}>Prior regime</th>
                <th style={S.th}>Regime</th>
              </tr>
            </thead>
            <tbody>
              {premium.rows.map((r: Row) => (
                <tr key={r.symbol}>
                  <td style={S.td}>{r.symbol}</td>
                  <td style={{ ...S.td, ...S.regime }}>{r.premium_prior ?? "—"}</td>
                  <td style={{ ...S.td, ...S.regime }}>{r.premium ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
                    {dollarsOrDash(r.gap_cents, currencySymbol)}
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
                <th style={S.thR}>{benchmarkLabel}</th>
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

      {/* Catalysts (M17) — one row per signal, already ordered by severity and
          already decayed by assembly. The archive shows the ref date and the
          tier, which the email condenses away. */}
      {catalysts && catalysts.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>Catalysts</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Symbol</th>
                <th style={S.th}>Source</th>
                <th style={S.th}>Signal</th>
                <th style={S.th}>Date</th>
                <th style={S.th}>Detail</th>
              </tr>
            </thead>
            <tbody>
              {catalysts.rows.map((r: Row, i: number) => (
                <tr key={`${r.symbol}-${r.kind}-${r.ref_date}-${i}`}>
                  <td style={S.td}>
                    {r.tier === "full" && <span style={{ color: "#8b2d2d" }}>⚠ </span>}
                    {r.symbol}
                  </td>
                  <td style={S.td}>{r.source}</td>
                  <td style={S.td}>{r.kind}</td>
                  <td style={S.td}>{r.ref_date ?? "—"}</td>
                  <td style={S.td}>
                    {catalystDetail(r, currencySymbol)}
                    {r.ambiguous_code && (
                      <span
                        style={S.provMarker}
                        title="vendor transaction type unrecognised; tax withholding and option
                               exercises could not be filtered (open question 2)"
                      >
                        {" "}
                        ?
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {catalysts.note && <p style={S.muted}>{catalysts.note}</p>}
        </section>
      )}

      {/* How they traded — the full technical snapshot. The email carries a
          six-column subset because 600px runs out; this page has no such
          limit, so it shows all three MA distances, both touch counts and the
          52-week pair. */}
      {tape && tape.rows.length > 0 && (
        <section style={S.card}>
          <h2 style={S.h2}>How they traded</h2>
          <table style={S.table}>
            <thead>
              <tr>
                <th style={S.th}>Symbol</th>
                <th style={S.thR}>RVOL</th>
                <th style={S.thR}>Vol wk</th>
                <th style={S.thR}>Vol mo</th>
                <th style={S.thR}>vs 20d</th>
                <th style={S.thR}>vs 50d</th>
                <th style={S.thR}>vs 200d</th>
                <th style={S.th}>MAs</th>
                <th style={S.thR}>Support</th>
                <th style={S.thR}>Resistance</th>
                <th style={S.thR}>52w range</th>
                <th style={S.th}>Range position</th>
              </tr>
            </thead>
            <tbody>
              {tape.rows.map((r: Row) => (
                <tr key={r.symbol}>
                  <td style={S.td}>
                    {r.symbol}
                    {r.breakout != null && (
                      <span style={S.breakout}>
                        {r.breakout === "up" ? " ▲ broke out" : " ▼ broke down"}
                      </span>
                    )}
                  </td>
                  <td style={S.tdR}>{multOrDash(r.rvol)}</td>
                  <td style={S.tdR}>{multOrDash(r.vol_vs_5d)}</td>
                  <td style={S.tdR}>{multOrDash(r.vol_vs_21d)}</td>
                  <td style={{ ...S.tdR, ...signColor(r.ma20_dist) }}>{pctOrDash(r.ma20_dist)}</td>
                  <td style={{ ...S.tdR, ...signColor(r.ma50_dist) }}>{pctOrDash(r.ma50_dist)}</td>
                  <td style={{ ...S.tdR, ...signColor(r.ma200_dist) }}>
                    {pctOrDash(r.ma200_dist)}
                  </td>
                  <td style={S.td}>{r.ma_stack ?? "—"}</td>
                  <td style={S.tdR}>
                    <Level
                      price={r.support}
                      touches={r.support_touches}
                      last={r.support_last_touch}
                    />
                  </td>
                  <td style={S.tdR}>
                    <Level
                      price={r.resistance}
                      touches={r.resistance_touches}
                      last={r.resistance_last_touch}
                    />
                  </td>
                  <td style={S.tdR}>
                    {r.low_52w != null && r.high_52w != null
                      ? `${r.low_52w.toFixed(2)} – ${r.high_52w.toFixed(2)}`
                      : "—"}
                  </td>
                  <td style={{ ...S.td, width: "18%" }}>
                    <RangeBar position={r.range_position} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* Where they stand (M20) — every name, full and brief tier alike. The
          email caps at three full-tier rows; the archive has no such limit,
          so it simply ignores the tier assembly assigned. The sparkline the
          spec calls for needs the 252-session price series, which the
          BriefObject doesn't carry (docs/02's single-contract rule) — deferred. */}
      {standing && (
        <section style={S.card}>
          <h2 style={S.h2}>
            Where they stand
            <span style={S.sectionNote}> · against each name's own year</span>
          </h2>
          {standing.rows.length === 0 ? (
            <p style={S.muted}>{standing.note}</p>
          ) : (
            <>
              <table style={S.table}>
                <thead>
                  <tr>
                    <th style={S.th}>Symbol</th>
                    <th style={S.thR}>RSI(14)</th>
                    <th style={S.thR}>MACD hist</th>
                    <th style={S.thR}>ADX(14)</th>
                    <th style={S.thR}>ATR %</th>
                    <th style={S.thR}>vs benchmark 21d</th>
                    <th style={S.th}>Divergence</th>
                  </tr>
                </thead>
                <tbody>
                  {standing.rows.map((r: Row) => (
                    <tr key={r.symbol}>
                      <td style={S.td}>{r.symbol}</td>
                      <ValueAndPctile value={r.rsi14} pctile={r.rsi14_pctile} places={1} />
                      <ValueAndPctile value={r.macd_hist} pctile={r.macd_hist_pctile} places={2} />
                      <ValueAndPctile value={r.adx14} pctile={r.adx14_pctile} places={1} />
                      <ValueAndPctile value={r.atr_pct} pctile={r.atr_pct_pctile} unsignedPercent />
                      <ValueAndPctile
                        value={r.rel_strength}
                        pctile={r.rel_strength_pctile}
                        percent
                        suffix={r.rel_strength_benchmark ? ` vs ${r.rel_strength_benchmark}` : ""}
                      />
                      <td style={S.td}>{r.divergence ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {/* The overflow note ("… also stretched — see the archive") tells
                  the email reader where to find the rest; the archive already
                  shows every row, so pointing it back at itself is nonsense
                  here. Any other note (e.g. "no name cleared the decile") is
                  still real information and renders as normal. */}
              {standing.note && !standing.note.endsWith("— see the archive") && (
                <p style={S.muted}>{standing.note}</p>
              )}
            </>
          )}
        </section>
      )}

      {/* Premium (M24) — every name, full and brief tier alike, ungated; the
          email shows only the full-tier rows. A measurement, never advice
          (D37). The strip grid itself is on /volatility; this carries the
          brief's one strip line per name. */}
      {premium && brief.kind !== "open" && (
        <section style={S.card}>
          <h2 style={S.h2}>
            Premium
            <span style={S.sectionNote}>
              {" "}
              · against each name's own year and the tape
              {premiumIsSynthetic && " · synthetic feed · not live prices"}
              {premiumIsStale && " · stale"}
            </span>
          </h2>
          {premium.rows.length === 0 ? (
            <p style={S.muted}>{premium.note}</p>
          ) : (
            <>
              <table style={S.table}>
                <thead>
                  <tr>
                    <th style={S.th}>Symbol</th>
                    <th style={S.th}>Regime</th>
                    <th style={S.thR}>IV30</th>
                    <th style={S.thR}>IV30 chg</th>
                    <th style={S.thR}>IV30 pctile</th>
                    <th style={S.thR}>IV rank</th>
                    <th style={S.thR}>HV20</th>
                    <th style={S.thR}>IV − HV</th>
                    <th style={S.thR}>IV/VIX</th>
                    <th style={S.thR}>Book median</th>
                    <th style={S.thR}>VIX</th>
                    <th style={S.thR}>VIX pctile</th>
                    <th style={S.thR}>Term slope</th>
                    <th style={S.thR}>Contracts writable</th>
                    <th style={S.th}>Earnings</th>
                    <th style={S.th}>Thin</th>
                    <th style={S.th}>Calls</th>
                  </tr>
                </thead>
                <tbody>
                  {premium.rows.map((r: Row) => (
                    <Fragment key={r.symbol}>
                      <tr>
                        <td style={S.td}>{r.symbol}</td>
                        <td style={{ ...S.td, ...S.regime }}>{r.premium ?? "—"}</td>
                        <td style={S.tdR}>
                          {numOrDash(r.iv30, 1)}
                          {r.iv30 != null && r.iv30_pctile == null && (
                            <span style={S.muted} title="percentile pending a full year">
                              †
                            </span>
                          )}
                        </td>
                        <td style={S.tdR}>{signedOrDash(r.iv30_chg, 1)}</td>
                        <td style={S.tdR}>{ordinalOrDash(r.iv30_pctile)}</td>
                        <td style={S.tdR}>{numOrDash(r.iv_rank_252, 2)}</td>
                        <td style={S.tdR}>{numOrDash(r.hv20, 1)}</td>
                        <td style={S.tdR}>{signedOrDash(r.iv_hv_spread, 1)}</td>
                        <td style={S.tdR}>{numOrDash(r.iv_vix_ratio, 2)}</td>
                        <td style={S.tdR}>{numOrDash(r.iv_vix_book_median, 2)}</td>
                        <td style={S.tdR}>{numOrDash(r.vix_close, 2)}</td>
                        <td style={S.tdR}>{ordinalOrDash(r.vix_pctile)}</td>
                        <td style={S.tdR}>{signedOrDash(r.iv_term_slope, 1)}</td>
                        <td style={S.tdR}>{r.contracts_writable ?? "—"}</td>
                        <td style={{ ...S.td, fontWeight: 400 }}>
                          {r.earnings_in_window ? (r.occurs_at ?? "—") : "—"}
                        </td>
                        <td style={{ ...S.td, fontWeight: 400 }}>{r.iv30_thin ? "thin" : "—"}</td>
                        <td style={{ ...S.td, fontWeight: 400 }}>
                          {r.calls != null ? "below" : stripLine(r)}
                        </td>
                      </tr>
                      {/* M26: one sub-row per selected call, every field the
                          body carries. v10 bodies have no `calls` and keep
                          the strip line in the cell above. */}
                      {r.calls != null && (
                        <tr>
                          <td colSpan={17} style={{ ...S.td, fontWeight: 400, paddingLeft: 16 }}>
                            <CallRows
                              calls={r.calls}
                              writable={r.contracts_writable}
                              currencySymbol={currencySymbol}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              {premiumPending && <p style={S.muted}>† percentile pending a full year</p>}
              {/* As §5: the overflow pointer is for the email reader only; the
                  no-data part of a combined note (M26) still shows here. */}
              {premiumArchiveNote(premium.note) && (
                <p style={S.muted}>{premiumArchiveNote(premium.note)}</p>
              )}
            </>
          )}
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

// A level and the evidence for it. Showing the price alone would be a line on
// a chart; the touch count and date are what make it a checkable claim.
function Level({
  price,
  touches,
  last,
}: {
  price: number | null | undefined;
  touches: number | null | undefined;
  last: string | null | undefined;
}) {
  if (price == null) return <span style={S.muted}>—</span>;
  return (
    <>
      {price.toFixed(2)}
      {touches != null && (
        <span style={S.levelEvidence}>
          {` ${touches}×`}
          {last ? ` · ${last.slice(5)}` : ""}
        </span>
      )}
    </>
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
function dollars(cents: number, symbol = "$"): string {
  return `${symbol}${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function signedDollars(cents: number, symbol = "$"): string {
  const sign = cents >= 0 ? "+" : "-";
  return `${sign}${symbol}${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
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
// ATR% is a magnitude (average true range as a percent of price), never
// negative — unlike rel_strength, it has no direction to sign.
function unsignedPct(fraction: number): string {
  return `${(fraction * 100).toFixed(2)}%`;
}
// The figures for one catalyst row. Every number comes from the object; a null
// renders as "size unknown" rather than dropping the row, because an unknown
// size is information and a missing row is not (open question 4).
function catalystDetail(r: Row, currencySymbol: string): string {
  const parts: string[] = [];
  if (r.insider_count != null) parts.push(`${r.insider_count} insiders`);
  if (r.value_cents != null) {
    parts.push(signedDollars(r.value_cents, currencySymbol).replace("+", ""));
  }
  if (r.pct_of_holding != null) parts.push(`${(r.pct_of_holding * 100).toFixed(1)}% of holding`);
  if (r.shares != null) parts.push(`${Math.round(r.shares).toLocaleString()} shares`);
  if (r.pct_of_float != null) parts.push(`${(r.pct_of_float * 100).toFixed(2)}% of float`);
  else if (r.source === "proposed") parts.push("size unknown");
  if (r.days_to_event != null) parts.push(`${r.days_to_event} sessions pre-earnings`);
  if (r.days_outstanding != null) parts.push(`${r.days_outstanding} days outstanding`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

// A value is only ever shown beside its percentile — the pair is the unit, and
// a bare oscillator value is what docs/01 was right to call soup. When the
// percentile is null (an incomplete baseline), the value goes dark too rather
// than showing a number with no comparison attached.
function ValueAndPctile({
  value,
  pctile,
  places = 2,
  percent = false,
  unsignedPercent = false,
  suffix = "",
}: {
  value: number | null | undefined;
  pctile: number | null | undefined;
  places?: number;
  percent?: boolean;
  unsignedPercent?: boolean;
  suffix?: string;
}) {
  if (value == null || pctile == null) return <td style={S.tdR}>—</td>;
  const shown = unsignedPercent ? unsignedPct(value) : percent ? pct(value) : value.toFixed(places);
  return (
    <td style={S.tdR}>
      {shown}
      {suffix}
      <span style={S.muted}> · {Math.round(pctile)}th</span>
    </td>
  );
}

function pctOrDash(fraction: number | null | undefined): string {
  return fraction == null ? "—" : pct(fraction);
}
function numOrDash(v: number | null | undefined, places: number): string {
  return v == null ? "—" : v.toFixed(places);
}
// U+2212 for minus, as `pct` and `signedAbs`.
function signedOrDash(v: number | null | undefined, places: number): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(places)}`;
}
function ordinalOrDash(p: number | null | undefined): string {
  if (p == null) return "—";
  const n = Math.round(p);
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  const suffix = ["th", "st", "nd", "rd"][n % 10] ?? "th";
  return `${n}${n % 10 <= 3 ? suffix : "th"}`;
}
// §6's note without its overflow sentence ("… — see the archive"), which is
// for the email reader; the "No option data for …" part stays (M26).
function premiumArchiveNote(note: string | null | undefined): string | null {
  if (!note) return null;
  const kept = note.split("; ").filter((part) => !part.endsWith("— see the archive"));
  return kept.length > 0 ? kept.join("; ") : null;
}
// The selected calls (M26): a nested table, one row per cell in the body's
// fixed order. Dollars per contract and across the position's writable
// contracts are measurements of what is held (D39); an empty cell says so.
function CallRows({
  calls,
  writable,
  currencySymbol,
}: {
  calls: CallQuote[];
  writable: number | null | undefined;
  currencySymbol: string;
}) {
  const label = (c: CallQuote) =>
    `${c.tenor}d ${c.rule === "atm" ? "ATM" : c.rule === "otm10" ? "10% OTM" : "10% ITM"}`;
  const strike = (v: number) => (Number.isInteger(v) ? `${v}` : v.toFixed(2));
  const fracOrDash = (v: number | null | undefined) =>
    v == null ? "—" : `${(v * 100).toFixed(1)}%`;
  const cell = { ...S.td, fontWeight: 400, borderBottom: "none", padding: "2px 10px 2px 0" };
  const cellR = { ...cell, textAlign: "right" as const };
  const head = { ...cell, color: "#888", fontSize: "0.78rem" };
  const headR = { ...head, textAlign: "right" as const };
  return (
    <table style={{ ...S.table, width: "auto" }}>
      <thead>
        <tr>
          <th style={head}>Call</th>
          <th style={head}>Contract</th>
          <th style={headR}>DTE</th>
          <th style={headR}>Mid</th>
          <th style={headR}>Mid chg</th>
          <th style={headR}>Per contract</th>
          <th style={headR}>Across {writable ?? "—"}</th>
          <th style={headR}>% of spot</th>
          <th style={headR}>chg</th>
          <th style={headR}>Time value</th>
          <th style={headR}>IV</th>
          <th style={headR}>IV chg</th>
          <th style={headR}>Δ</th>
          <th style={headR}>OI</th>
          <th style={head}>Marks</th>
        </tr>
      </thead>
      <tbody>
        {calls.map((c) => (
          <tr key={`${c.tenor}-${c.rule}`}>
            <td style={cell}>{label(c)}</td>
            {c.contract == null ? (
              <td style={cell} colSpan={14}>
                no two-sided quote
              </td>
            ) : (
              <>
                <td style={cell}>
                  {c.strike != null && c.expiry != null
                    ? `${strike(c.strike)}C ${c.expiry}`
                    : c.contract}
                </td>
                <td style={cellR}>{c.dte ?? "—"}</td>
                <td style={cellR}>
                  {c.mid != null ? `${currencySymbol}${c.mid.toFixed(2)}` : "—"}
                </td>
                <td style={cellR}>
                  {dollarsOrDash(c.mid_chg != null ? c.mid_chg * 100 : null, currencySymbol)}
                </td>
                <td style={cellR}>
                  {c.contract_cents != null ? dollars(c.contract_cents, currencySymbol) : "—"}
                </td>
                <td style={cellR}>
                  {c.writable_cents != null ? dollars(c.writable_cents, currencySymbol) : "—"}
                </td>
                <td style={cellR}>{fracOrDash(c.premium_pct)}</td>
                <td style={cellR}>
                  {c.premium_pct_chg != null ? `${signedOrDash(c.premium_pct_chg * 100, 1)}%` : "—"}
                </td>
                <td style={cellR}>{fracOrDash(c.time_value_pct)}</td>
                <td style={cellR}>{numOrDash(c.iv, 1)}</td>
                <td style={cellR}>{signedOrDash(c.iv_chg, 1)}</td>
                <td style={cellR}>{numOrDash(c.delta, 2)}</td>
                <td style={cellR}>
                  {c.open_interest != null ? c.open_interest.toLocaleString("en-US") : "—"}
                </td>
                <td style={cell}>
                  {[
                    c.target_miss ? "strike off target" : null,
                    c.tenor_collapsed ? "same expiry as shorter tenor" : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || "—"}
                </td>
              </>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
// §6's strip line, worded as the email's: the call mid as a fraction of spot,
// and in brackets its relative change against the prior session at the same
// tenor (`chg_tenor_pct`). The 7-day cell only when assembly carried it.
function stripLine(r: Row): string {
  if (r.call_atm30_pct == null && r.call_otm30_pct == null) return "—";
  const cell = (v: number | null | undefined, chg: number | null | undefined) =>
    `${v == null ? "—" : `${(v * 100).toFixed(1)}%`}${chg == null ? "" : ` (${signedOrDash(chg * 100, 1)}%)`}`;
  const parts = [
    `30d ATM pays ${cell(r.call_atm30_pct, r.call_atm30_chg)}`,
    `10% OTM ${cell(r.call_otm30_pct, r.call_otm30_chg)}`,
  ];
  if (r.call_atm7_pct != null) parts.push(`7d ATM ${cell(r.call_atm7_pct, null)}`);
  return parts.join(" · ");
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
function dollarsOrDash(cents: number | null | undefined, symbol = "$"): string {
  if (cents == null) return "—";
  const sign = cents >= 0 ? "+" : "−";
  return `${sign}${symbol}${(Math.abs(cents) / 100).toFixed(2)}`;
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
  levelEvidence: { color: "#666", fontSize: "0.78rem" },
  breakout: { fontWeight: 600, fontSize: "0.78rem" },
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
  // §6's regime word: muted, never pine/oxblood — a measurement, not a signal.
  regime: { fontWeight: 400, color: "#666" },
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
