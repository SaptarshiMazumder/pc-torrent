import { formatCredits } from "../../utils/creditsFormat";
import { formatDurationLabel } from "../../utils/jobUtils";

// Per-GPU usage for IN-FLIGHT renders — time + cost + chunk count, ranked by
// time (bar width = share of the busiest GPU).  Live-only by design: past
// renders carry no per-chunk GPU, so this shows an idle note when nothing's
// rendering rather than pretending to have lifetime history.
export default function GpuUsagePanel({ gpus }) {
  const maxSeconds = gpus.reduce((m, g) => Math.max(m, g.seconds), 0) || 1;
  return (
    <div className="tele-gpu-panel">
      <div className="tele-panel-head">
        <h3>GPUs in use</h3>
        <span className="tele-panel-sub">live</span>
      </div>
      {gpus.length === 0 ? (
        <div className="tele-gpu-empty">No active renders — GPU usage appears here while you’re rendering.</div>
      ) : (
        <ul className="tele-gpu-list">
          {gpus.map((g) => (
            <li key={g.name} className="tele-gpu-row">
              <div className="tele-gpu-row-head">
                <span className="tele-gpu-name">{g.name}</span>
                <span className="tele-gpu-meta">
                  {formatDurationLabel(g.seconds)} · {formatCredits(g.credits)} tok
                </span>
              </div>
              <div className="tele-gpu-bar">
                <span style={{ width: `${Math.max(4, (g.seconds / maxSeconds) * 100)}%` }} />
              </div>
              <div className="tele-gpu-chunks">{g.chunks} chunk{g.chunks === 1 ? "" : "s"}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
