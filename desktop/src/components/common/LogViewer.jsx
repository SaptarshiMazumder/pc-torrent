import { useRef, useEffect } from "react";

const LEVEL_COLORS = {
  info: "var(--muted)",
  warn: "#f59e0b",
  error: "#ef4444",
};

const SOURCE_COLORS = {
  agent: "var(--th)",
  container: "#12a150",
  setup: "#f5a623",
  app: "#3f74ff",
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
            style={{ color: SOURCE_COLORS[entry.source] || "var(--muted)" }}
          >
            [{entry.source}]
          </span>
          <span
            className="log-message"
            style={{ color: LEVEL_COLORS[entry.level] || "var(--muted)" }}
          >
            {entry.message}
          </span>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
