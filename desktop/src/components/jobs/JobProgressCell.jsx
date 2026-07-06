import {
  jobProgressPct,
  jobRenderedFrames,
  jobTotalFrames,
} from "../../utils/jobUtils";

// Progress bar + "rendered/total · pct".  Read-only; all from the list DTO.
// The fill turns green at 100% (a purely visual Aurora Glass touch); the
// rendered/total · pct readout is unchanged.
export default function JobProgressCell({ job }) {
  const pct = Math.min(100, Math.max(0, Math.round(jobProgressPct(job))));
  const rendered = jobRenderedFrames(job);
  const total = jobTotalFrames(job);
  const done = job?.status === "done";
  return (
    <div className="job-progress-cell">
      <div className="job-progress-track">
        <div
          className={`job-progress-fill${done ? " progress-done" : ""}`}
          style={{ width: `${done ? 100 : pct}%` }}
        />
      </div>
      <span className="job-progress-text">
        {rendered}/{total} · {done ? 100 : pct}%
      </span>
    </div>
  );
}
