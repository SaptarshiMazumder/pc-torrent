import { STATUS_LABELS } from "../../utils/jobUtils";

// Status pill.  Reuses the existing `status-<status>` colour classes (same
// ones the card grid uses) so colours stay consistent across views.
export default function JobStatusBadge({ status }) {
  const s = status || "pending";
  return (
    <span className={`job-status-badge status-${s}`}>
      {s === "running" && <span className="status-badge-dot" />}
      {STATUS_LABELS[s] || s}
    </span>
  );
}
