import { useLayoutEffect, useRef, useState } from "react";

function rangeLabel(task) {
  const start = task.frame_start;
  const end = task.frame_end;
  if (start == null || end == null) return "—";
  return start === end ? `${start}` : `${start}-${end}`;
}

function CompletedChunkCell({ task }) {
  const machine = task.machine_gpu || "Unknown";
  const tick = "#22c55e";

  return (
    <div className="cc-cell">
      <div className="cc-cell-head">
        <svg
          className="cc-cell-tick"
          width="16" height="16" viewBox="0 0 24 24" fill="none"
          stroke={tick} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ filter: `drop-shadow(0 0 4px ${tick}55)` }}
        >
          <circle cx="12" cy="12" r="10" />
          <polyline points="17 9 11 15 8 12" />
        </svg>
        <span className="cc-cell-title">{rangeLabel(task)}</span>
        <span className="cc-cell-machine" title={machine}>{machine}</span>
      </div>
    </div>
  );
}

export default function CompletedChunksPanel({ tasks }) {
  const completed = (tasks || [])
    .filter((t) => t.status === "done")
    .slice()
    .sort((a, b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0));

  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const [rowHeight, setRowHeight] = useState(0);
  const [visibleInFirstRow, setVisibleInFirstRow] = useState(0);
  const gridRef = useRef(null);

  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) {
      setOverflows(false);
      return;
    }
    const measure = () => {
      const children = el.children;
      if (children.length === 0) {
        setOverflows(false);
        setRowHeight(0);
        setVisibleInFirstRow(0);
        return;
      }
      const first = children[0];
      const rh = first.offsetHeight;
      const firstTop = first.offsetTop;
      let count = 0;
      for (const child of children) {
        if (child.offsetTop === firstTop) count += 1;
        else break;
      }
      setRowHeight(rh);
      setVisibleInFirstRow(count);
      setOverflows(children.length > count);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [completed.length]);

  const collapsed = overflows && !expanded;
  const gridStyle = collapsed && rowHeight > 0
    ? { maxHeight: rowHeight, overflow: "hidden" }
    : undefined;

  return (
    <div className="inst-panel">
      <div className="inst-panel-header">
        <div className="inst-panel-title-row">
          <div className="inst-panel-icon">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
          </div>
          <span className="inst-panel-title">Completed Chunks</span>
          <span className="inst-panel-count">{completed.length}</span>
        </div>
      </div>

      {completed.length === 0 ? (
        <div className="muted" style={{ padding: "12px 4px", fontSize: 13 }}>
          No chunks completed yet.
        </div>
      ) : (
        <>
          <div ref={gridRef} className="cc-grid" style={gridStyle}>
            {completed.map((task) => (
              <CompletedChunkCell key={task.job_id} task={task} />
            ))}
          </div>
          {overflows && (
            <button
              type="button"
              className="cc-show-more"
              onClick={() => setExpanded((v) => !v)}
            >
              <svg
                width="14" height="14" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.5"
                style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s" }}
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
              <span>{expanded ? "Show less" : `Show ${Math.max(completed.length - visibleInFirstRow, 0)} more`}</span>
            </button>
          )}
        </>
      )}
    </div>
  );
}
