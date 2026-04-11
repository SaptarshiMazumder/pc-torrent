const SEGMENT_COLORS = [
  "#e8724a",
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#ef4444",
  "#8b5cf6",
];

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").trim();
}

export default function SegmentedProgressBar({ tasks, totalFrames }) {
  if (!tasks || tasks.length === 0 || !totalFrames) return null;

  const activeTasks = tasks.filter((t) => t.status !== "failed");
  if (activeTasks.length === 0) return null;

  return (
    <div className="segmented-progress-wrap">
      <div className="segmented-progress-track">
        {activeTasks.map((task, i) => {
          const segWidth = totalFrames > 0 ? (task.total_frames / totalFrames) * 100 : 0;
          const fillPct = task.status === "done" ? 100
            : typeof task.progress_pct === "number" ? Math.max(0, Math.min(100, task.progress_pct))
            : 0;
          const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
          const isFailed = task.status === "failed";
          const isPending = task.status === "pending";

          return (
            <div
              key={task.job_id}
              className={`segmented-progress-segment ${isFailed ? "segment-failed" : ""}`}
              style={{ width: `${Math.max(segWidth, 2)}%` }}
              title={`${gpuShortName(task.machine_gpu)}: frames ${task.frame_start}-${task.frame_end} (${task.rendered_frames || 0}/${task.total_frames})`}
            >
              <div
                className="segmented-progress-fill"
                style={{
                  width: `${fillPct}%`,
                  background: isFailed
                    ? "repeating-linear-gradient(45deg,#ef4444 0,#ef4444 4px,#7f1d1d 4px,#7f1d1d 8px)"
                    : isPending ? "rgba(180,175,220,0.1)" : `linear-gradient(90deg, ${color}, ${color}cc)`,
                  opacity: isPending ? 0.3 : 1,
                }}
              />
            </div>
          );
        })}
      </div>

      <div className="segmented-progress-labels">
        {activeTasks.map((task, i) => {
          const color = SEGMENT_COLORS[i % SEGMENT_COLORS.length];
          const pct = task.status === "done" ? 100
            : typeof task.progress_pct === "number" ? Math.round(task.progress_pct) : 0;
          return (
            <div key={task.job_id} className="segmented-label-item">
              <span className="segmented-label-dot" style={{ background: task.status === "failed" ? "#ef4444" : color, boxShadow: `0 0 8px ${task.status === "failed" ? "#ef444466" : color + "44"}` }} />
              <span className="segmented-label-gpu">{gpuShortName(task.machine_gpu)}</span>
              <span className="segmented-label-frames">{task.frame_start}&ndash;{task.frame_end}</span>
              <span className={`segmented-label-pct ${task.status === "failed" ? "pct-failed" : task.status === "done" ? "pct-done" : ""}`}>
                {task.status === "failed" ? "Failed" : task.status === "pending" ? "Waiting" : `${pct}%`}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
