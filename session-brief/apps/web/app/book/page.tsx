import { DEV_USER_ID } from "@/lib/constants";
import { db } from "@/lib/db";
import { holdings, lots, sectors } from "@/lib/db/schema";
import { asc, eq } from "drizzle-orm";
import {
  closeLot,
  createHolding,
  createLot,
  createSector,
  deleteHolding,
  deleteLot,
  deleteSector,
} from "./actions";

export const dynamic = "force-dynamic";

function dollars(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export default async function BookPage() {
  const [sectorRows, holdingRows, lotRows] = await Promise.all([
    db
      .select()
      .from(sectors)
      .where(eq(sectors.userId, DEV_USER_ID))
      .orderBy(asc(sectors.sortOrder), asc(sectors.name)),
    db
      .select()
      .from(holdings)
      .where(eq(holdings.userId, DEV_USER_ID))
      .orderBy(asc(holdings.symbol)),
    db.select().from(lots).where(eq(lots.userId, DEV_USER_ID)).orderBy(asc(lots.openedOn)),
  ]);

  const holdingsBySector = new Map<string, typeof holdingRows>();
  for (const h of holdingRows) {
    const list = holdingsBySector.get(h.sectorId) ?? [];
    list.push(h);
    holdingsBySector.set(h.sectorId, list);
  }
  const lotsByHolding = new Map<string, typeof lotRows>();
  for (const l of lotRows) {
    const list = lotsByHolding.get(l.holdingId) ?? [];
    list.push(l);
    lotsByHolding.set(l.holdingId, list);
  }

  const today = new Date().toISOString().slice(0, 10);

  return (
    <main style={S.main}>
      <h1 style={{ margin: 0 }}>Book</h1>
      <p style={S.muted}>
        Sectors → holdings → lots. Data lives in Postgres and survives redeploys.
      </p>

      {/* Add sector */}
      <form action={createSector} style={S.addRow}>
        <input
          name="name"
          placeholder="Sector name (e.g. Semiconductors)"
          required
          style={S.input}
        />
        <input
          name="benchmark_symbol"
          placeholder="Benchmark (e.g. SMH)"
          style={{ ...S.input, width: 160 }}
        />
        <button type="submit" style={S.btn}>
          Add sector
        </button>
      </form>

      {sectorRows.length === 0 && <p style={S.muted}>No sectors yet. Add one above.</p>}

      {sectorRows.map((sector) => {
        const sectorHoldings = holdingsBySector.get(sector.id) ?? [];
        return (
          <section key={sector.id} style={S.sector}>
            <div style={S.sectorHead}>
              <h2 style={{ margin: 0, fontSize: "1.15rem" }}>
                {sector.name}
                {sector.benchmarkSymbol && <span style={S.badge}>{sector.benchmarkSymbol}</span>}
              </h2>
              <form action={deleteSector}>
                <input type="hidden" name="id" value={sector.id} />
                <button type="submit" style={S.linkBtn}>
                  delete sector
                </button>
              </form>
            </div>

            {/* Add holding */}
            <form action={createHolding} style={S.addRow}>
              <input type="hidden" name="sector_id" value={sector.id} />
              <input
                name="symbol"
                placeholder="Symbol (e.g. NVDA)"
                required
                style={{ ...S.input, width: 140 }}
              />
              <select name="status" style={S.input} defaultValue="owned">
                <option value="owned">owned</option>
                <option value="watching">watching</option>
              </select>
              <button type="submit" style={S.btn}>
                Add holding
              </button>
            </form>

            {sectorHoldings.map((holding) => {
              const holdingLots = lotsByHolding.get(holding.id) ?? [];
              return (
                <div key={holding.id} style={S.holding}>
                  <div style={S.holdingHead}>
                    <strong>{holding.symbol}</strong>
                    <span style={S.muted}>{holding.status}</span>
                    <form action={deleteHolding}>
                      <input type="hidden" name="id" value={holding.id} />
                      <button type="submit" style={S.linkBtn}>
                        delete
                      </button>
                    </form>
                  </div>

                  {holdingLots.length > 0 && (
                    <table style={S.table}>
                      <thead>
                        <tr>
                          <th style={S.th}>Shares</th>
                          <th style={S.th}>Cost/sh</th>
                          <th style={S.th}>Opened</th>
                          <th style={S.th}>Closed</th>
                          <th style={S.th} />
                        </tr>
                      </thead>
                      <tbody>
                        {holdingLots.map((lot) => (
                          <tr key={lot.id}>
                            <td style={S.td}>{lot.shares}</td>
                            <td style={S.td}>{dollars(lot.costBasisCents)}</td>
                            <td style={S.td}>{lot.openedOn}</td>
                            <td style={S.td}>
                              {lot.closedOn ?? (
                                <form action={closeLot} style={{ display: "flex", gap: 4 }}>
                                  <input type="hidden" name="id" value={lot.id} />
                                  <input
                                    type="date"
                                    name="closed_on"
                                    required
                                    style={{ ...S.input, padding: "2px 4px" }}
                                    defaultValue={today}
                                  />
                                  <button type="submit" style={S.linkBtn}>
                                    close
                                  </button>
                                </form>
                              )}
                            </td>
                            <td style={S.td}>
                              <form action={deleteLot}>
                                <input type="hidden" name="id" value={lot.id} />
                                <button type="submit" style={S.linkBtn}>
                                  delete
                                </button>
                              </form>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}

                  {/* Add lot */}
                  <form action={createLot} style={S.addRow}>
                    <input type="hidden" name="holding_id" value={holding.id} />
                    <input
                      name="shares"
                      type="number"
                      step="any"
                      min="0"
                      placeholder="Shares"
                      required
                      style={{ ...S.input, width: 100 }}
                    />
                    <input
                      name="cost_basis"
                      type="number"
                      step="0.01"
                      min="0"
                      placeholder="Cost/sh ($)"
                      required
                      style={{ ...S.input, width: 110 }}
                    />
                    <input
                      name="opened_on"
                      type="date"
                      required
                      defaultValue={today}
                      style={S.input}
                    />
                    <button type="submit" style={S.btn}>
                      Add lot
                    </button>
                  </form>
                </div>
              );
            })}
          </section>
        );
      })}
    </main>
  );
}

const S: Record<string, React.CSSProperties> = {
  main: { fontFamily: "system-ui", padding: "2rem", maxWidth: 820, margin: "0 auto" },
  muted: { color: "#666", fontSize: "0.85rem", margin: 0 },
  addRow: { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "10px 0" },
  input: { padding: "6px 8px", border: "1px solid #ccc", borderRadius: 6, fontSize: "0.9rem" },
  btn: {
    padding: "6px 12px",
    border: "1px solid #333",
    background: "#333",
    color: "#fff",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: "0.9rem",
  },
  linkBtn: {
    border: "none",
    background: "none",
    color: "#a00",
    cursor: "pointer",
    fontSize: "0.8rem",
    padding: 0,
  },
  sector: { border: "1px solid #e2e2e2", borderRadius: 10, padding: "14px 16px", margin: "16px 0" },
  sectorHead: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  badge: {
    marginLeft: 8,
    fontSize: "0.7rem",
    background: "#eef",
    color: "#335",
    padding: "2px 6px",
    borderRadius: 4,
    verticalAlign: "middle",
  },
  holding: { borderTop: "1px solid #eee", paddingTop: 10, marginTop: 10 },
  holdingHead: { display: "flex", gap: 10, alignItems: "center" },
  table: { borderCollapse: "collapse", margin: "8px 0", fontSize: "0.85rem" },
  th: {
    textAlign: "left",
    padding: "3px 12px 3px 0",
    color: "#888",
    fontWeight: 500,
    borderBottom: "1px solid #eee",
  },
  td: { padding: "3px 12px 3px 0", borderBottom: "1px solid #f4f4f4" },
};
