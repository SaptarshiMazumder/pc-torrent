import { usePendingQueue } from "../../hooks/usePendingQueue";

function rangeLabel(item) {
  const start = item.frame_start;
  const end = item.frame_end;
  if (start == null || end == null) return "—";
  return start === end ? `${start}` : `${start}-${end}`;
}

function PendingChunkCell({ item }) {
  const color = "#a5b4fc";
  return (
    <div className="pq-cell">
      <div className="pq-cell-head">
        <svg
          className="pq-cell-icon"
          width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ filter: `drop-shadow(0 0 4px ${color}55)` }}
        >
          <circle cx="12" cy="12" r="10" />
          <polyline points="12 6 12 12 16 14" />
        </svg>
        <span className="pq-cell-title">{rangeLabel(item)}</span>
        <span className="pq-cell-meta">
          {item.type === "retry_chunk" ? "retry" : "queued"}
          {typeof item.attempt === "number" ? ` · #${item.attempt}` : ""}
        </span>
      </div>
    </div>
  );
}

export default function PendingChunksPanel({ backendUrl, groupId, tasks, totalFrames, groupStatus }) {
  const { items, loading, stopped, expectsPending, refresh } = usePendingQueue(
    backendUrl, groupId, { tasks, totalFrames, groupStatus },
  );

  let body;
  if (items.length > 0) {
    body = (
      <div className="pq-grid">
        {items.map((item) => (
          <PendingChunkCell key={item.id} item={item} />
        ))}
      </div>
    );
  } else if (!expectsPending) {
    body = (
      <div className="muted" style={{ padding: "12px 4px", fontSize: 13 }}>
        No items pending.
      </div>
    );
  } else if (stopped) {
    body = (
      <div className="muted" style={{ padding: "12px 4px", fontSize: 13 }}>
        Stopped polling — chunk likely lost. Use Refresh to try again.
      </div>
    );
  } else {
    body = (
      <div className="muted" style={{ padding: "12px 4px", fontSize: 13 }}>
        {loading ? "Checking..." : "Waiting for queue..."}
      </div>
    );
  }

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
          </div>
          <span className="inst-panel-title">Pending Chunks</span>
          {items.length > 0 && (
            <span className="inst-panel-count">{items.length}</span>
          )}
          <button
            type="button"
            className="pq-refresh"
            onClick={refresh}
            disabled={loading}
            title="Refresh pending queue"
          >
            <svg
              width="13" height="13" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2.5"
              style={{ opacity: loading ? 0.4 : 1 }}
            >
              <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
              <path d="M21 3v5h-5" />
            </svg>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>
      {body}
    </div>
  );
}
