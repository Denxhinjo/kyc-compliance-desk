/**
 * Shown while the stats queries run.
 *
 * Next.js renders this immediately and swaps in the real page when the Server
 * Component's data arrives. The skeleton mirrors the real layout so nothing
 * moves when it does — a spinner in the middle of an empty page tells the
 * visitor less and makes the arrival feel like a jump.
 */
export default function Loading() {
  return (
    <main>
      <p className="crumb">&nbsp;</p>
      <h1>Stats</h1>
      <p className="sub">Reading figures from the database…</p>
      <div className="stat-grid">
        {Array.from({ length: 6 }).map((_, i) => (
          <div className="stat" key={i}>
            <span className="skeleton line short" />
            <span className="skeleton line big" />
            <span className="skeleton line" />
          </div>
        ))}
      </div>
    </main>
  );
}
