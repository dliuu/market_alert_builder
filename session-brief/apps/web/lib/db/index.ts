import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

// The web app reads/writes rows only; the worker owns the schema (D5).
// Transaction pooler (6543) → prepare:false. Strip SQLAlchemy's "+psycopg" tag.
const raw = process.env.DATABASE_POOL_URL;
if (!raw) throw new Error("DATABASE_POOL_URL is not set (see repo-root .env)");
const url = raw.replace("postgresql+psycopg://", "postgresql://");

// Reuse one client across dev HMR reloads so we don't exhaust pooled connections.
const globalForDb = globalThis as unknown as {
  pg?: ReturnType<typeof postgres>;
};

const client = globalForDb.pg ?? postgres(url, { prepare: false, ssl: "require" });
if (process.env.NODE_ENV !== "production") globalForDb.pg = client;

export const db = drizzle(client, { schema });
