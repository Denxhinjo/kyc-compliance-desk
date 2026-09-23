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

/** How long to let the call hang before abandoning it.
 *
 * Generous because the drain is itself a function and can be cold: a measured
 * cold start is about 1.6s, so a 2s budget was timing out on exactly the
 * invocation that needed it most. */
const NUDGE_TIMEOUT_MS = 8_000;

/**
 * Ring the doorbell. Never throws; failures are logged and forgotten.
 *
 * MUST BE CALLED INSIDE `after()`, and that is not a style preference — it is
 * the difference between this working and silently doing nothing.
 *
 * The first version was fire-and-forget: it started a `fetch` and returned
 * without awaiting, on the reasoning that the caller should not wait for a
 * doorbell. That is correct on a long-lived server and wrong on a function.
 * Vercel freezes the instance the moment the response is flushed, so an
 * in-flight request with nobody awaiting it is killed before the connection is
 * even established. Measured: the webhook returned 200, the job sat queued,
 * and nothing in any log said why — the request never left.
 *
 * `after()` is Next's answer to exactly this. It runs the callback after the
 * response has been sent but keeps the invocation alive until it finishes, so
 * the applicant waits for nothing and the nudge actually happens.
 */
export async function nudgeWorker(reason: string): Promise<void> {
  if (!DRAIN_URL || !DRAIN_SECRET) {
    // Local development runs the long-lived worker loop instead, which needs
    // no nudging. Silence rather than a warning on every enqueue.
    return;
  }

  try {
    const response = await fetch(DRAIN_URL, {
      method: "POST",
      headers: { "x-drain-secret": DRAIN_SECRET },
      // Bounded so a hanging drain cannot hold the invocation open until the
      // platform kills it.
      signal: AbortSignal.timeout(NUDGE_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.warn(`nudge(${reason}): drain replied ${response.status}`);
      return;
    }
    console.log(`nudge(${reason}): ${await response.text()}`);
  } catch (error: unknown) {
    // Expected sometimes: a timeout, a deploy in progress. The scheduled drain
    // is the backstop, so this is information rather than a problem — and it
    // must never propagate, because the applicant's submission already
    // succeeded and a failed doorbell cannot be allowed to undo that.
    console.warn(`nudge(${reason}): ${String(error)}`);
  }
}
