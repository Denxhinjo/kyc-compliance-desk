import { spawnSync } from "node:child_process";

/**
 * Verify that a production build compiles, without touching .next.
 *
 * Exists because `next build` and `next dev` share an output directory: running
 * a build while the dev server is up leaves the running server referencing
 * chunks that no longer exist, and every page 500s with MODULE_NOT_FOUND. The
 * page code is fine; the artifacts underneath it have been replaced.
 *
 * A plain `NEXT_DIST_DIR=… next build` would do, but npm runs scripts through
 * cmd.exe on Windows, where that is not valid syntax. Hence a launcher.
 */
const result = spawnSync("npx", ["next", "build"], {
  stdio: "inherit",
  shell: true,
  env: { ...process.env, NEXT_DIST_DIR: ".next-check" },
});

process.exit(result.status ?? 1);
