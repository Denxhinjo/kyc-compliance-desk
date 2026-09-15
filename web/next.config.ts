import type { NextConfig } from "next";
import { config as loadEnv } from "dotenv";
import path from "node:path";

// Next.js only auto-loads .env files from this directory (/web). We keep a
// single source of truth at the repo root instead, so /web, /worker and
// docker-compose can never drift apart. This loads it before the app boots.
loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const nextConfig: NextConfig = {};

export default nextConfig;
