// Phase 1 placeholder.  Phase 3 will wire this to the per-user Redis-backed
// pending queue endpoint, with auto-refresh every 10s when frontend math
// says there are non-running, non-terminal chunks for this group.

export default function PendingChunksPanel({ groupId, tasks }) {
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
        </div>
      </div>
      <div className="muted" style={{ padding: "12px 4px", fontSize: 13 }}>
        No items pending.
      </div>
    </div>
  );
}
