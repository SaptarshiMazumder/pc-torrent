import { useState } from "react";
import { useTranslation } from "react-i18next";
import { jobStatusLabel, resolveJobFilename, jobKey } from "../../utils/jobUtils";
import JobThumbnail from "./JobThumbnail";
import { useError } from "../../contexts/ErrorContext";

function formatRelativeDate(isoString, t) {
  if (!isoString) return "";
  const date = new Date(isoString);
  const now = Date.now();
  const diff = now - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return t("time.justNow");
  if (minutes < 60) return t("time.minutesAgo", { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("time.hoursAgo", { count: hours });
  const days = Math.floor(hours / 24);
  if (days < 7) return t("time.daysAgo", { count: days });
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
  const { t } = useTranslation(["myJobs", "common"]);
  const [removing, setRemoving] = useState(false);
  const id = jobKey(job);
  const displayName = resolveJobFilename(job);
  const status = job?.status || "pending";
  const { showError } = useError();

  const handleRemove = async (e) => {
    e.stopPropagation();
    if (removing) return;
    setRemoving(true);
    try {
      await onRemove(id);
    } catch (err) {
      showError({
        title: t("deleteError.title"),
        message: err?.message || t("deleteError.message"),
        detail: err?.body || null,
      });
    } finally {
      setRemoving(false);
    }
  };

  // Backend snapshots `tasks_count` onto terminal groups so the list view
  // can render this label without re-fetching the per-chunk tasks array.
  const taskCount =
    typeof job?.tasks_count === "number"
      ? job.tasks_count
      : Array.isArray(job?.tasks)
        ? job.tasks.length
        : 0;
  // Freshly submitted groups (and any group whose pending allocation
  // request the daemon hasn't yet promoted to dispatch) have no tasks
  // attached.  Render "Queued" instead of "0 machines" so the empty
  // state reads as intentional rather than broken.
  const isTerminal = ["done", "failed", "cancelled"].includes(status);
  const machineLabel = job?.group_id
    ? (taskCount === 0 && !isTerminal
        ? t("card.queued")
        : t("machineCount", { count: taskCount }))
    : job?.machine_gpu || t("machineCount", { count: 1 });

  return (
    <button
      type="button"
      className="job-grid-card"
      onClick={() => onClick(id)}
      title={displayName}
    >
      <div className="job-grid-thumb-wrap">
        <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />

        <span
          role="button"
          tabIndex={0}
          className={`job-grid-delete-btn${removing ? " removing" : ""}`}
          onClick={handleRemove}
          onKeyDown={(e) => e.key === "Enter" && handleRemove(e)}
          title={t("card.deleteJob")}
        >
          {removing ? (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="8 6" strokeLinecap="round"><animateTransform attributeName="transform" type="rotate" from="0 7 7" to="360 7 7" dur="0.7s" repeatCount="indefinite"/></circle></svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3.5 4h7M5.5 4V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1M6 6.5v3M8 6.5v3M4.5 4l.5 7a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1l.5-7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          )}
        </span>

        <span className={`job-grid-status-pill status-${status}`}>
          {status === "running" && <span className="status-badge-dot" />}
          {jobStatusLabel(status)}
        </span>
      </div>

      <div className="job-grid-info">
        <span className="job-grid-title">{displayName}</span>
        <div className="job-grid-meta">
          <span className="job-grid-meta-item">{machineLabel}</span>
          <span className="job-grid-meta-sep">&middot;</span>
          <span className="job-grid-meta-item">
            {formatDuration(job?.submitted_at, job?.completed_at)}
          </span>
          <span className="job-grid-meta-date">{formatRelativeDate(job?.submitted_at, t)}</span>
        </div>
      </div>
    </button>
  );
}
