import { timingSafeEqual } from "node:crypto";
import { renderBrief } from "@/emails/render";
import type { BriefObject } from "@/lib/contracts/brief";
import { db } from "@/lib/db";
import { briefs } from "@/lib/db/schema";
import { eq } from "drizzle-orm";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

// The one service-to-service call (docs/02 #4): the worker POSTs a brief id with a
// shared secret and gets { html, text } back. Pure — brief in, strings out, no writes.
export async function POST(_req: Request, { params }: { params: Promise<{ briefId: string }> }) {
  const secret = process.env.RENDER_SHARED_SECRET;
  if (!secret) {
    return NextResponse.json({ error: "render secret not configured" }, { status: 500 });
  }
  if (!authorized(_req.headers.get("authorization"), secret)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const { briefId } = await params;
  const [row] = await db.select().from(briefs).where(eq(briefs.id, briefId)).limit(1);
  if (!row) {
    return NextResponse.json({ error: "brief not found" }, { status: 404 });
  }

  const brief = row.body as unknown as BriefObject;
  // `kind` selects the template — the open and close briefs are different jobs
  // (docs/01), but the worker still makes exactly one render call (D6).
  const { html, text } = await renderBrief(brief);
  return NextResponse.json({ html, text });
}

function authorized(header: string | null, secret: string): boolean {
  if (!header?.startsWith("Bearer ")) return false;
  const provided = Buffer.from(header.slice(7));
  const expected = Buffer.from(secret);
  return provided.length === expected.length && timingSafeEqual(provided, expected);
}
