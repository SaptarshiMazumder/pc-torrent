import {
  jobProgressPct,
  jobRenderedFrames,
  jobTotalFrames,
} from "../../utils/jobUtils";

// Progress bar + "rendered/total · pct".  Read-only; all from the list DTO.
export default function JobProgressCell({ job }) {
  const pct = Math.min(100, Math.max(0, Math.round(jobProgressPct(job))));
  const rendered = jobRenderedFrames(job);
  const total = jobTotalFrames(job);
  return (
    <div className="job-progress-cell">
      <div className="job-progress-track">
        <div className="job-progress-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="job-progress-text">
        {rendered}/{total} · {pct}%
      </span>
    </div>
  );
}
