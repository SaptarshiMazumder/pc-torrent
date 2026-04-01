import LogViewer from "../components/LogViewer";

export default function LogsPage({ logs, onClearLogs }) {
  return (
    <div className="page logs-page">
      <div className="page-header">
        <h2>Logs</h2>
        <div className="page-header-actions">
          <span className="log-count">{logs.length} entries</span>
          <button
            className="btn btn-secondary"
            onClick={() => onClearLogs?.()}
            disabled={logs.length === 0}
          >
            Clear Logs
          </button>
        </div>
      </div>
      <LogViewer logs={logs} />
    </div>
  );
}
