import Link from "next/link";

/**
 * Shown for a bad URL, and — more usefully — for an application id that does
 * not exist, which is what notFound() raises in the status and case pages.
 *
 * Deliberately says nothing about whether the id was merely malformed or
 * genuinely absent. An application id is a bearer token in this system: anyone
 * holding it can see the status page. Distinguishing "no such case" from
 * "not yours" would let someone probe for which ids exist.
 */
export default function NotFound() {
  return (
    <main>
      <h1>Not found</h1>
      <p className="sub">
        There is nothing at this address. If you followed a link to an
        application, check that the id is complete.
      </p>
      <div className="panel">
        <p>
          <Link className="button" href="/">
            Back to the start
          </Link>
        </p>
      </div>
    </main>
  );
}
