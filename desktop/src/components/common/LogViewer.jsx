import { useRef, useEffect } from "react";

const LEVEL_COLORS = {
  info: "#a1a1aa",
  warn: "#f59e0b",
  error: "#ef4444",
};

const SOURCE_COLORS = {
  agent: "#e8724a",
  container: "#22c55e",
  setup: "#f5a623",
  app: "#3b82f6",
};

export default function LogViewer({ logs }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs.length]);

  if (logs.length === 0) {
    return (
      <div className="log-viewer">
        <div className="log-empty">No logs yet</div>
      </div>
    );
  }

  return (
    <div className="log-viewer">
      {logs.map((entry, i) => (
        <div key={i} className={`log-line level-${entry.level}`}>
          <span
            className="log-source"
            style={{ color: SOURCE_COLORS[entry.source] || "#a1a1aa" }}
          >
            [{entry.source}]
          </span>
          <span
            className="log-message"
            style={{ color: LEVEL_COLORS[entry.level] || "#a1a1aa" }}
          >
            {entry.message}
          </span>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
