import { jobKey } from "../../utils/jobUtils";
import SortableHeaderCell from "./SortableHeaderCell";
import JobTableRow from "./JobTableRow";

// The table shell -- pure layout, holds no state.  Renders headers + rows
// from the column descriptors; sort comes in, onSort goes out.
export default function JobsTable({
  jobs,
  columns,
  sort,
  onSort,
  onSelect,
  backendUrl,
  authToken,
}) {
  return (
    <div className="jobs-table-wrap">
      <table className="jobs-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <SortableHeaderCell
                key={col.key}
                column={col}
                sort={sort}
                onSort={onSort}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <JobTableRow
              key={jobKey(job)}
              job={job}
              columns={columns}
              backendUrl={backendUrl}
              authToken={authToken}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
