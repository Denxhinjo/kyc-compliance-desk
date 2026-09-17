/**
 * The case skeleton keeps the score block and the evidence grid in place.
 *
 * Worth the detail here: an officer opens hundreds of these a day, and a layout
 * that settles differently each time is a layout you cannot build muscle memory
 * against.
 */
export default function Loading() {
  return (
    <main className="case">
      <div className="case-head">
        <div className="case-head-left">
          <p className="crumb">&nbsp;</p>
          <span className="skeleton line big" />
          <span className="skeleton line short" />
        </div>
        <div className="case-head-right">
          <span className="skeleton block score-skeleton" />
        </div>
      </div>
      <section className="flags">
        <h2>Why this was flagged</h2>
        <span className="skeleton line" />
        <span className="skeleton line" />
      </section>
      <div className="case-grid">
        {Array.from({ length: 3 }).map((_, i) => (
          <section className="panel-block" key={i}>
            <span className="skeleton line short" />
            <span className="skeleton line" />
            <span className="skeleton line" />
          </section>
        ))}
      </div>
    </main>
  );
}
