import type { NextConfig } from "next";
import { config as loadEnv } from "dotenv";
import path from "node:path";

// Next.js only auto-loads .env files from this directory (/web). We keep a
// single source of truth at the repo root instead, so /web, /worker and
// docker-compose can never drift apart. This loads it before the app boots.
loadEnv({ path: path.resolve(process.cwd(), "..", ".env") });

const nextConfig: NextConfig = {
  // `next build` and `next dev` both write to .next by default, so building
  // while the dev server is running overwrites the chunks it is serving and
  // every page starts failing with "Cannot find module './vendor-chunks/…'".
  // Allowing the directory to be overridden lets `npm run build:check` verify a
  // production build without disturbing a running dev server. Deployment leaves
  // this unset and uses .next as normal.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
