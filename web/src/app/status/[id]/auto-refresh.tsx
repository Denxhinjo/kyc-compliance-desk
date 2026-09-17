"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * Re-render the server component every few seconds so the applicant sees
 * progress without pressing anything.
 *
 * router.refresh() re-runs the Server Component on the server and patches the
 * result into the page. It is not a full reload: scroll position, focus and any
 * client state survive, which is why this is worth doing rather than a
 * <meta http-equiv="refresh">.
 *
 * Polling is the honest choice here. Server-sent events or websockets would
 * remove the delay, but the applicant is waiting on a human-scale process —
 * seconds to minutes — so a few seconds of latency costs nothing and this adds
 * no infrastructure. The same trade-off as the job queue polling in Phase 2.
 */
export function AutoRefresh({ seconds = 4 }: { seconds?: number }) {
  const router = useRouter();

  useEffect(() => {
    const timer = setInterval(() => router.refresh(), seconds * 1000);
    // Cleanup matters: without it, navigating away leaves the interval running
    // and every visit adds another one.
    return () => clearInterval(timer);
  }, [router, seconds]);

  return null;
}
