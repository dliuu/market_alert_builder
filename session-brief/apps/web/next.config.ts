import { config as loadEnv } from "dotenv";
import type { NextConfig } from "next";

// Secrets live in the repo-root .env (shared with the worker); the web app has
// no .env of its own. Load it so server actions can reach Postgres.
loadEnv({ path: "../../.env" });

const nextConfig: NextConfig = {};

export default nextConfig;
