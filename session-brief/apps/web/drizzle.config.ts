import { config as loadEnv } from "dotenv";
import { defineConfig } from "drizzle-kit";

// Schema lives in the worker's Postgres; the web app only introspects it (D5).
// Never generate/push migrations from here — Alembic is the sole DDL path.
loadEnv({ path: "../../.env" });

export default defineConfig({
  dialect: "postgresql",
  schema: "./lib/db/schema.ts",
  out: "./lib/db",
  schemaFilter: ["public"],
  dbCredentials: {
    // Session pooler (5432) for introspection — behaves like a direct connection.
    // Strip SQLAlchemy's "+psycopg" driver tag; drizzle-kit wants a plain URL.
    url: (process.env.DATABASE_URL ?? "").replace("postgresql+psycopg://", "postgresql://"),
  },
});
