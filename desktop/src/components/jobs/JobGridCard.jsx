import { useState } from "react";
import { STATUS_LABELS, resolveJobFilename, jobKey } from "../../utils/jobUtils";
import JobThumbnail from "./JobThumbnail";

function formatRelativeDate(isoString) {
  if (!isoString) return "";
  const date = new Date(isoString);
  const now = Date.now();
  const diff = now - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

function formatDuration(startIso, endIso) {
  const start = Date.parse(startIso || "");
  if (!Number.isFinite(start)) return "";
  const end = endIso ? Date.parse(endIso) : Date.now();
  if (!Number.isFinite(end)) return "";
  const diffMs = Math.max(0, end - start);
  const totalSec = Math.floor(diffMs / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hrs < 24) return remMins > 0 ? `${hrs}h ${remMins}m` : `${hrs}h`;
  const days = Math.floor(hrs / 24);
  const remHrs = hrs % 24;
  return remHrs > 0 ? `${days}d ${remHrs}h` : `${days}d`;
}

export default function JobGridCard({ job, authToken, backendUrl, onClick, onRemove }) {
  const [removing, setRemoving] = useState(false);
  const id = jobKey(job);
  const displayName = resolveJobFilename(job);
  const status = job?.status || "pending";

  const handleRemove = async (e) => {
    e.stopPropagation();
    if (removing) return;
    setRemoving(true);
    try {
      await onRemove(id);
    } finally {
      setRemoving(false);
    }
  };

  return (
    <button
      type="button"
      className="job-grid-card"
      onClick={() => onClick(id)}
      title={displayName}
    >
      <div className="job-grid-thumb-wrap">
        <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />

        <div className="job-grid-overlay">
          <div className="job-grid-overlay-top">
            <span
              role="button"
              tabIndex={0}
              className={`job-grid-delete-btn${removing ? " removing" : ""}`}
              onClick={handleRemove}
              onKeyDown={(e) => e.key === "Enter" && handleRemove(e)}
              title="Delete job"
            >
              {removing ? (
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="8 6" strokeLinecap="round"><animateTransform attributeName="transform" type="rotate" from="0 7 7" to="360 7 7" dur="0.7s" repeatCount="indefinite"/></circle></svg>
              ) : (
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3.5 4h7M5.5 4V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1M6 6.5v3M8 6.5v3M4.5 4l.5 7a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1l.5-7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              )}
            </span>
            <span className={`job-grid-status-pill status-${status}`}>
              {status === "running" && <span className="status-badge-dot" />}
              {STATUS_LABELS[status] || status}
            </span>
          </div>
          <div className="job-grid-name-gradient">
            <span className="job-grid-name-text">{displayName}</span>
          </div>
        </div>
      </div>

      <div className="job-grid-footer">
        <span className={`job-grid-footer-status status-${status}`}>
          {STATUS_LABELS[status] || status}
        </span>
        {job?.submitted_at && (
          <span className="job-grid-duration" title="Duration">
            {formatDuration(job.submitted_at, job.completed_at)}
          </span>
        )}
        <span className="job-grid-date">{formatRelativeDate(job?.submitted_at)}</span>
      </div>
    </button>
  );
}
