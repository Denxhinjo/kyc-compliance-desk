/** Skeleton rows matching the queue, so the table does not jump on arrival. */
export default function Loading() {
  return (
    <main className="desk-main">
      <div className="desk-head">
        <h1>Pending review</h1>
        <div className="desk-stats">
          <span className="skeleton line short" />
        </div>
      </div>
      <table className="grid queue">
        <thead>
          <tr>
            <th className="col-wait">Waiting</th>
            <th className="col-score">Score</th>
            <th className="col-name">Applicant</th>
            <th className="col-country">Country</th>
            <th>Top flag</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: 8 }).map((_, i) => (
            <tr key={i}>
              <td><span className="skeleton line" /></td>
              <td><span className="skeleton line" /></td>
              <td><span className="skeleton line" /></td>
              <td><span className="skeleton line" /></td>
              <td><span className="skeleton line" /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
