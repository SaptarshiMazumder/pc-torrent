import { jobKey } from "../../utils/jobUtils";

// One job row.  Field-agnostic: iterates the column descriptors and renders
// each column's Cell.  The whole row is clickable -> onSelect(id), which
// opens the existing JobDetailView (unchanged flow).
export default function JobTableRow({ job, columns, backendUrl, authToken, onSelect }) {
  const id = jobKey(job);
  return (
    <tr className="job-tr" onClick={() => onSelect(id)}>
      {columns.map((col) => (
        <td key={col.key} className={`job-td align-${col.align || "left"}`}>
          {col.Cell({ job, backendUrl, authToken })}
        </td>
      ))}
    </tr>
  );
}
