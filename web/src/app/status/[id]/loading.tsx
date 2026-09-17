/** What the applicant sees for the moment before their status arrives. */
export default function Loading() {
  return (
    <main>
      <p className="crumb">&nbsp;</p>
      <h1>Your application</h1>
      <p className="sub">Loading…</p>
      <div className="panel">
        <span className="skeleton line big" />
        <span className="skeleton line" />
      </div>
    </main>
  );
}
