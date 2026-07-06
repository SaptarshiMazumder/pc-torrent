import { jobStatusLabel } from "../../utils/jobUtils";

// Status pill.  Reuses the existing `status-<status>` colour classes (same
// ones the card grid uses) so colours stay consistent across views.  The
// label comes from jobStatusLabel (i18n-backed); this stays subscribed to
// language changes via its ancestor page's useTranslation re-render.
export default function JobStatusBadge({ status }) {
  const s = status || "pending";
  return (
    <span className={`job-status-badge status-${s}`}>
      {s === "running" && <span className="status-badge-dot" />}
      {jobStatusLabel(s)}
    </span>
  );
}
