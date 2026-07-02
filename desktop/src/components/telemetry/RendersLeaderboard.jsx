import { useMemo, useState } from "react";
import { formatCredits } from "../../utils/creditsFormat";
import { formatBytesLabel, formatDurationLabel, formatCompactNumber } from "../../utils/jobUtils";

// The metric a render is ranked + bar-scaled by.  Each owns its own formatter
// so the same row renders the right units per selection.
const METRICS = [
  { key: "frames", label: "Frames", format: (v) => v.toLocaleString() },
  { key: "seconds", label: "Time", format: (v) => formatDurationLabel(v) },
  { key: "credits", label: "Cost", format: (v) => `${formatCredits(v)} tok` },
  { key: "bytes", label: "Scene size", format: (v) => formatBytesLabel(v) },
  { key: "verts", label: "Geometry", format: (v) => `${formatCompactNumber(v)} verts` },
];
const TOP_N = 8;

// Ranked renders, re-sortable by any metric — the reference's leaderboard.
// The bar length is the value's share of the current leader, so switching the
// metric re-orders and re-scales in one move.
export default function RendersLeaderboard({ rows }) {
  const [metricKey, setMetricKey] = useState("frames");
  const metric = METRICS.find((m) => m.key === metricKey) || METRICS[0];

  const ranked = useMemo(
    () => rows.slice().sort((a, b) => b[metric.key] - a[metric.key]).slice(0, TOP_N),
    [rows, metric.key],
  );
  const max = ranked.reduce((m, r) => Math.max(m, r[metric.key]), 0) || 1;

  return (
    <div className="tele-board">
      <div className="tele-board-head">
        <h3>Renders</h3>
        <div className="tele-metric-seg" role="tablist">
          {METRICS.map((m) => (
            <button
              key={m.key}
              type="button"
              role="tab"
              aria-selected={m.key === metricKey}
              className={`tele-metric-opt${m.key === metricKey ? " active" : ""}`}
              onClick={() => setMetricKey(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>
      {ranked.length === 0 ? (
        <div className="tele-board-empty">No renders yet.</div>
      ) : (
        <ol className="tele-board-list">
          {ranked.map((r, i) => (
            <li key={r.id || i} className="tele-board-row">
              <span className="tele-board-rank">{i + 1}</span>
              <span className="tele-board-name" title={r.name}>{r.name}</span>
              <span className="tele-board-bar">
                <span style={{ width: `${Math.max(3, (r[metric.key] / max) * 100)}%` }} />
              </span>
              <span className="tele-board-val">{metric.format(r[metric.key])}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
