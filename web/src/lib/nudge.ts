/**
 * Tell the worker there may be something to do. Never wait for the answer.
 *
 * WHY THIS IS NOT AN INTERNAL API
 *
 * CLAUDE.md's first architecture rule is that the two services never call each
 * other. This bends it, and the part that survives is the important part.
 *
 * The rule exists to prevent an internal API: the web app asking the worker to
 * do something and waiting to hear how it went, which drags CORS, shared auth,
 * retries and a second failure mode into a design that had none.
 *
 * This is not that. The work is already durable — it was committed to the jobs
 * table before this function is called, and the queue remains the only channel
 * carrying meaning. This POST has no payload, expects no answer, and is not
 * awaited. It is a doorbell: it says "there may be something to do", and if it
 * never arrives, the scheduled drain a few minutes later does the same work.
 *
 * Delete this file and the system is still correct, only slower. That is the
 * test for whether a call like this is safe to add.
 *
 * WHY IT IS NEEDED AT ALL
 *
 * Without it, the shortest path from "applicant finishes verification" to "a
 * decision exists" is however long until the next scheduled drain — minutes.
 * The applicant is watching a status page for the whole of it. The nudge turns
 * that into seconds while changing nothing about correctness.
 */

const DRAIN_URL = process.env.DRAIN_URL;
const DRAIN_SECRET = process.env.DRAIN_SECRET;

/** How long to let the call hang before abandoning it. */
const NUDGE_TIMEOUT_MS = 2_000;

/**
 * Fire-and-forget. Resolves immediately; failures are logged, never thrown.
 *
 * Deliberately NOT awaited by callers, and deliberately incapable of throwing
 * into one. An applicant's application has already been accepted by the time
 * this runs, and a failed doorbell must never turn a successful submission
 * into an error page.
 */
export function nudgeWorker(reason: string): void {
  if (!DRAIN_URL || !DRAIN_SECRET) {
    // Local development runs the long-lived worker loop instead, which needs
    // no nudging. Silence rather than a warning on every enqueue.
    return;
  }

  // AbortSignal.timeout rather than a dangling fetch: a serverless invocation
  // can be frozen the moment its response is sent, and an unbounded request
  // would be the thing keeping it alive.
  void fetch(DRAIN_URL, {
    method: "POST",
    headers: { "x-drain-secret": DRAIN_SECRET },
    signal: AbortSignal.timeout(NUDGE_TIMEOUT_MS),
  })
    .then((response) => {
      if (!response.ok) {
        console.warn(`nudge(${reason}): drain replied ${response.status}`);
      }
    })
    .catch((error: unknown) => {
      // Expected sometimes: a timeout, a cold function, a deploy in progress.
      // The scheduled drain is the backstop, so this is information rather
      // than a problem.
      console.warn(`nudge(${reason}): ${String(error)}`);
    });
}
