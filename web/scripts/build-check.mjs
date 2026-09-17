import { spawnSync } from "node:child_process";
import { rmSync } from "node:fs";

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
 *
 * The output directory is removed before AND after. Next.js adds
 * `.next-check/types` to tsconfig's `include` when it builds there, so a
 * leftover directory means the editor and `tsc --noEmit` typecheck against the
 * route validator from an OLD build alongside the current one. The symptom is
 * a baffling error about a route not satisfying the constraint `"/"` in a
 * generated file nobody wrote.
 */

const OUT = ".next-check";
rmSync(OUT, { recursive: true, force: true });
const result = spawnSync("npx", ["next", "build"], {
  stdio: "inherit",
  shell: true,
  env: { ...process.env, NEXT_DIST_DIR: ".next-check" },
});

// Leave nothing behind for tsc to find.
rmSync(OUT, { recursive: true, force: true });

process.exit(result.status ?? 1);
