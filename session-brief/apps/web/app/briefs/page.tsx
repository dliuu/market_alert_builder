import { DEV_USER_ID } from "@/lib/constants";
import { db } from "@/lib/db";
import { briefs } from "@/lib/db/schema";
import { desc, eq } from "drizzle-orm";

export const dynamic = "force-dynamic";

export default async function BriefsIndex() {
  const rows = await db
    .select({ sessionDate: briefs.sessionDate, kind: briefs.kind })
    .from(briefs)
    .where(eq(briefs.userId, DEV_USER_ID))
    .orderBy(desc(briefs.sessionDate), desc(briefs.kind));

  return (
    <main style={{ fontFamily: "system-ui", padding: "2rem", maxWidth: 820, margin: "0 auto" }}>
      <h1 style={{ margin: 0 }}>Briefs</h1>
      <p style={{ color: "#666", fontSize: "0.85rem" }}>
        The archive. Each brief renders from its stored BriefObject.
      </p>
      {rows.length === 0 && (
        <p style={{ color: "#666" }}>
          No briefs yet. Assemble one:{" "}
          <code>uv run -m worker.cli brief --kind close --date &lt;date&gt;</code>
        </p>
      )}
      <ul style={{ lineHeight: 1.9 }}>
        {rows.map((r) => (
          <li key={`${r.sessionDate}-${r.kind}`}>
            <a href={`/briefs/${r.sessionDate}-${r.kind}`}>
              {r.sessionDate} · {r.kind}
            </a>
          </li>
        ))}
      </ul>
    </main>
  );
}
